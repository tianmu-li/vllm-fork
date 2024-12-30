# Adapted from
# https://github.com/huggingface/transformers/blob/v4.28.0/src/transformers/models/llama/modeling_llama.py
# Copyright 2023 The vLLM team.
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
# Copyright 2024 Habana Labs, Ltd. an Intel Company
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Inference-only LLaMA model compatible with HuggingFace weights."""
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Type, Union
import os

import torch
from torch import nn
from transformers import LlamaConfig

from vllm.attention import Attention, AttentionMetadata
from vllm.compilation.decorators import support_torch_compile
from vllm.config import CacheConfig, VllmConfig
from vllm.distributed import (get_pp_group, get_tensor_model_parallel_rank,
                              get_tensor_model_parallel_world_size)
from vllm.model_executor.layers.activation import SiluAndMul
from vllm.model_executor.layers.layernorm import RMSNorm
from vllm.model_executor.layers.linear import (MergedColumnParallelLinear,
                                               QKVParallelLinear,
                                               ColumnParallelLinear,
                                               RowParallelLinear)
from vllm.model_executor.layers.logits_processor import LogitsProcessor
from vllm.model_executor.layers.quantization import QuantizationConfig
from vllm.model_executor.layers.quantization.compressed_tensors.utils import (
    get_compressed_tensors_cache_scale)
from vllm.model_executor.layers.rotary_embedding import get_rope
from vllm.model_executor.layers.sampler import SamplerOutput, get_sampler
from vllm.model_executor.layers.vocab_parallel_embedding import (
    DEFAULT_VOCAB_PADDING_SIZE, ParallelLMHead, VocabParallelEmbedding)
from vllm.model_executor.model_loader.weight_utils import (
    default_weight_loader, kv_cache_scales_loader, maybe_remap_kv_scale_name)
from vllm.model_executor.sampling_metadata import SamplingMetadata
from vllm.platforms import current_platform
from vllm.sequence import IntermediateTensors

from .interfaces import SupportsLoRA, SupportsPP
from .utils import (AutoWeightsLoader, PPMissingLayer, extract_layer_index,
                    is_pp_missing_parameter,
                    make_empty_intermediate_tensors_factory, make_layers,
                    maybe_prefix)

is_hpu = current_platform.is_hpu()

# split_size>128: fixed-length splits (each slice is split_size)
# split_size<128: fixed-num splits (split_size num of slices)
def get_split_size(seq_len, batch_size, orig_split_size):
    if orig_split_size<128:
        split_size = max((seq_len*batch_size)//orig_split_size, 1)
    else:
        split_size = orig_split_size
    return split_size

VLLM_MLP_SIZE_OVERRIDE = int(os.environ.get("VLLM_MLP_SIZE_OVERRIDE", "512"))

class LlamaMLP(nn.Module):

    def __init__(
        self,
        hidden_size: int,
        intermediate_size: int,
        hidden_act: str,
        quant_config: Optional[QuantizationConfig] = None,
        bias: bool = False,
        prefix: str = "",
        do_split: bool = False,
        split_size: int = 2,
        split_gate_up: bool = False
    ) -> None:
        super().__init__()
        self.split_gate_up = split_gate_up
        self.hidden_size = hidden_size
        if self.split_gate_up:
            self.gate_proj = ColumnParallelLinear(
                input_size=hidden_size,
                output_size=intermediate_size,
                bias=bias,
                quant_config=quant_config,
                prefix=f"{prefix}.gate_proj",
            )
            self.up_proj = ColumnParallelLinear(
                input_size=hidden_size,
                output_size=intermediate_size,
                bias=bias,
                quant_config=quant_config,
                prefix=f"{prefix}.up_proj"
            )
        else:
            self.gate_up_proj = MergedColumnParallelLinear(
                input_size=hidden_size,
                output_sizes=[intermediate_size] * 2,
                bias=bias,
                quant_config=quant_config,
                prefix=f"{prefix}.gate_up_proj",
            )
        self.down_proj = RowParallelLinear(
            input_size=intermediate_size,
            output_size=hidden_size,
            bias=bias,
            quant_config=quant_config,
            prefix=f"{prefix}.down_proj",
            # do_split=do_split,
            # split_size=split_size
        )
        if hidden_act != "silu":
            raise ValueError(f"Unsupported activation: {hidden_act}. "
                             "Only silu is supported for now.")
        self.act_fn = SiluAndMul()

    def forward(self, x, skip_seq_split=False):
        batch_size = x.size(0)
        seq_len = x.size(1)
        if (seq_len*batch_size)%512==0:
            x = x.view(-1,512,self.hidden_size)
        if self.split_gate_up:
            x = nn.functional.silu(self.gate_proj(x)[0]) * self.up_proj(x)[0]
        else:
            x, _ = self.gate_up_proj(x)
            x = self.act_fn(x)

        # Separate split for down is not implemented yet
        x, _ = self.down_proj(x)

        if (seq_len*batch_size)%512==0:
            x = x.view(batch_size,seq_len,self.hidden_size)
        return x


class LlamaAttention(nn.Module):

    def __init__(
        self,
        config: LlamaConfig,
        hidden_size: int,
        num_heads: int,
        num_kv_heads: int,
        rope_theta: float = 10000,
        rope_scaling: Optional[Dict[str, Any]] = None,
        max_position_embeddings: int = 8192,
        quant_config: Optional[QuantizationConfig] = None,
        bias: bool = False,
        cache_config: Optional[CacheConfig] = None,
        prefix: str = "",
        do_split: bool = False,
        split_size: int = 2,
        output_slice: bool = False
    ) -> None:
        super().__init__()
        layer_idx = extract_layer_index(prefix)
        self.hidden_size = hidden_size
        tp_size = get_tensor_model_parallel_world_size()
        self.total_num_heads = num_heads
        assert self.total_num_heads % tp_size == 0
        self.num_heads = self.total_num_heads // tp_size
        self.total_num_kv_heads = num_kv_heads
        if self.total_num_kv_heads >= tp_size:
            # Number of KV heads is greater than TP size, so we partition
            # the KV heads across multiple tensor parallel GPUs.
            assert self.total_num_kv_heads % tp_size == 0
        else:
            # Number of KV heads is less than TP size, so we replicate
            # the KV heads across multiple tensor parallel GPUs.
            assert tp_size % self.total_num_kv_heads == 0
        self.num_kv_heads = max(1, self.total_num_kv_heads // tp_size)
        # MistralConfig has an optional head_dim introduced by Mistral-Nemo
        self.head_dim = getattr(config, "head_dim",
                                self.hidden_size // self.total_num_heads)
        self.q_size = self.num_heads * self.head_dim
        self.kv_size = self.num_kv_heads * self.head_dim
        self.scaling = self.head_dim**-0.5
        self.rope_theta = rope_theta
        self.max_position_embeddings = max_position_embeddings
        self.split_qk_v = cache_config.split_qk_v
        self.do_split = do_split
        self.split_size = split_size
        self.output_slice = output_slice

        if self.split_qk_v:
            self.q_proj = ColumnParallelLinear(input_size=self.hidden_size,
                                               output_size=self.hidden_size,
                                               bias=bias,
                                               gather_output=False,
                                               skip_bias_add=False,
                                               params_dtype=None,
                                               quant_config=quant_config,
                                               prefix=f"{prefix}.q_proj")
            self.k_proj = ColumnParallelLinear(input_size=self.hidden_size,
                                               output_size=self.kv_size * tp_size,
                                               bias=bias,
                                               gather_output=False,
                                               skip_bias_add=False,
                                               params_dtype=None,
                                               quant_config=quant_config,
                                               prefix=f"{prefix}.k_proj")
            self.v_proj = ColumnParallelLinear(input_size=self.hidden_size,
                                               output_size=self.kv_size * tp_size,
                                               bias=bias,
                                               gather_output=False,
                                               skip_bias_add=False,
                                               params_dtype=None,
                                               quant_config=quant_config,
                                               prefix=f"{prefix}.v_proj")
        else:
            self.qkv_proj = QKVParallelLinear(
                hidden_size=hidden_size,
                head_size=self.head_dim,
                total_num_heads=self.total_num_heads,
                total_num_kv_heads=self.total_num_kv_heads,
                bias=bias,
                quant_config=quant_config,
                prefix=f"{prefix}.qkv_proj"
            )

        self.o_proj = RowParallelLinear(
            input_size=self.total_num_heads * self.head_dim,
            output_size=hidden_size,
            bias=bias,
            quant_config=quant_config,
            prefix=f"{prefix}.o_proj",
            # do_split=do_split,
            # split_size=split_size,
        )

        is_neox_style = True
        if quant_config is not None and quant_config.get_name() == "gguf":
            is_neox_style = False

        self.rotary_emb = get_rope(
            self.head_dim,
            rotary_dim=self.head_dim,
            max_position=max_position_embeddings,
            base=rope_theta,
            rope_scaling=rope_scaling,
            is_neox_style=is_neox_style,
        )

        if hasattr(config, "interleaved_sliding_window"):
            if isinstance(config.interleaved_sliding_window, int):
                sliding_window = config.interleaved_sliding_window
            elif isinstance(config.interleaved_sliding_window, list):
                sw_idx = layer_idx % len(config.interleaved_sliding_window)
                sliding_window = config.interleaved_sliding_window[sw_idx]
            else:
                raise ValueError(f"{type(sliding_window)} is not supported.")
        else:
            sliding_window = None

        self.attn = Attention(
            self.num_heads,
            self.head_dim,
            self.scaling,
            num_kv_heads=self.num_kv_heads,
            cache_config=cache_config,
            quant_config=quant_config,
            per_layer_sliding_window=sliding_window,
            prefix=f"{prefix}.attn",
        )

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: torch.Tensor,
        attn_metadata: AttentionMetadata,
        skip_seq_split: bool = False,
        **kwargs,
    ) -> torch.Tensor:
        batch_size = hidden_states.size(0)
        seq_len = hidden_states.size(1)
        split_size = get_split_size(seq_len, batch_size, self.split_size)
        do_split = self.do_split and attn_metadata.is_prompt
        if self.split_qk_v:
            # q, k, v, _ = self.qkv_proj(hidden_states)
            q, _ = self.q_proj(hidden_states)
            k, _ = self.k_proj(hidden_states)
            v, _ = self.v_proj(hidden_states)
        else:
            qkv, _ = self.qkv_proj(hidden_states)
            q, k, v = qkv.split([self.q_size, self.kv_size, self.kv_size],
                                dim=-1)
        q, k = self.rotary_emb(positions, q, k)
        attn_output = self.attn(q, k, v, kv_cache, attn_metadata, **kwargs)
        if ((seq_len*batch_size)%split_size==0) and do_split:
            attn_output = attn_output.view(-1, split_size, self.q_size)
            attn_list = torch.split(attn_output, 1)
            output_list = []
            for attn_slice in attn_list:
                output_list.append(self.o_proj(attn_slice)[0])
            if self.output_slice:
                return output_list
            else:
                output = torch.cat(output_list)
                output = output.view(batch_size, seq_len, self.hidden_size)
                return output
        else:
            output, _ = self.o_proj(attn_output)
            return output


class LlamaDecoderLayer(nn.Module):

    def __init__(
        self,
        config: LlamaConfig,
        cache_config: Optional[CacheConfig] = None,
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
    ) -> None:
        super().__init__()
        self.tp_parallel_size = int(os.environ.get("VLLM_TP_SPLIT_SIZE_BY_BATCH", 1))
        self.layer_idx = int(prefix.split('.')[2])
        self.hidden_size = config.hidden_size
        self.total_num_layers = 32
        rope_theta = getattr(config, "rope_theta", 10000)
        rope_scaling = getattr(config, "rope_scaling", None)
        if rope_scaling is not None and getattr(
                config, "original_max_position_embeddings", None):
            rope_scaling["original_max_position_embeddings"] = (
                config.original_max_position_embeddings)
        max_position_embeddings = getattr(config, "max_position_embeddings",
                                          8192)
        # Support abacusai/Smaug-72B-v0.1 with attention_bias
        # Support internlm/internlm-7b with bias
        attention_bias = getattr(config, "attention_bias", False) or getattr(
            config, "bias", False)

        split_size = int(os.environ.get('VLLM_TP_SPLIT_SIZE_BY_SEQ', '1'))
        output_slice = int(os.environ.get('OUTPUT_SLICE', '1')) == 1
        enable_o_proj_split = int(os.environ.get('VLLM_TP_O_PROJ_SPLIT_ENABLE', '1')) == 1
        do_split = split_size > 1
        self.split_size = split_size
        self.do_split = do_split
        self.output_slice = output_slice and do_split
        self.self_attn = LlamaAttention(
            config=config,
            hidden_size=self.hidden_size,
            num_heads=config.num_attention_heads,
            num_kv_heads=getattr(config, "num_key_value_heads",
                                 config.num_attention_heads),
            rope_theta=rope_theta,
            rope_scaling=rope_scaling,
            max_position_embeddings=max_position_embeddings,
            quant_config=quant_config,
            bias=attention_bias,
            cache_config=cache_config,
            prefix=f"{prefix}.self_attn",
            do_split=do_split and enable_o_proj_split,
            split_size=split_size,
            output_slice=output_slice
        )
        self.mlp = LlamaMLP(
            hidden_size=self.hidden_size,
            intermediate_size=config.intermediate_size,
            hidden_act=config.hidden_act,
            quant_config=quant_config,
            bias=getattr(config, "mlp_bias", False),
            prefix=f"{prefix}.mlp",
            do_split=do_split,
            split_size=split_size,
            split_gate_up=cache_config.split_gate_up,
        )
        self.input_layernorm = RMSNorm(config.hidden_size,
                                       eps=config.rms_norm_eps)
        self.post_attention_layernorm = RMSNorm(config.hidden_size,
                                                eps=config.rms_norm_eps)

    def set_total_num_layers(self, total_num_layers: int) -> None:
        self.total_num_layers = total_num_layers

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: torch.Tensor,
        attn_metadata: AttentionMetadata,
        residual: Optional[torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        if isinstance(hidden_states, torch.Tensor):
            skip_split = hidden_states.size()[0] == 1
            shape_total = 1
            for i in hidden_states.shape:
                shape_total = shape_total * i
            skip_split = skip_split or (shape_total < 1024 * 4 * 8192)
        else:
            skip_split = False
        if not attn_metadata.is_prompt or \
            self.tp_parallel_size == 1 or skip_split:
            # Self Attention
            if residual is None:
                residual = hidden_states
                hidden_states = self.input_layernorm(hidden_states)
            else:
                hidden_states, residual = self.input_layernorm(
                    hidden_states, residual)
            hidden_states_shape = hidden_states.shape
            batch_size, seq_len, hidden_size = hidden_states_shape

            split_size = get_split_size(seq_len, batch_size, self.split_size)
            # only split for prefill
            do_split = self.do_split and attn_metadata.is_prompt
            
            hidden_states = self.self_attn(positions=positions,
                                           hidden_states=hidden_states,
                                           kv_cache=kv_cache,
                                           attn_metadata=attn_metadata)
            
            # self_attn output a list of tensors to be processed sequential at layernorm and mlp
            if do_split and (seq_len*batch_size)%split_size==0 and self.output_slice:
                # Slice residual
                residual_shape = residual.shape
                residual = residual.view(-1, split_size, hidden_size)
                residual_list = torch.split(residual, 1)
                residual_list_output = []
                output_list = []
                # Sequentially process slices
                for hidden_state, residual in zip(hidden_states, residual_list):
                    hidden_state, residual = self.post_attention_layernorm(hidden_state, residual)
                    # hidden_state = self.mlp.forward_pre_down(hidden_state)
                    hidden_state = self.mlp(hidden_state)
                    residual_list_output.append(residual)
                    output_list.append(hidden_state)
                # Combine slices
                residual = torch.cat(residual_list_output).view(*residual_shape)
                hidden_states = torch.cat(output_list).view(batch_size, seq_len, -1)
                # hidden_states = self.mlp.forward_down_proj(hidden_states)
            else:
                # Fully Connected
                hidden_states, residual = self.post_attention_layernorm(
                    hidden_states, residual)
                hidden_states = self.mlp(hidden_states)
        else:
            hidden_states, residual = self.try_split_forward(
                positions, hidden_states, kv_cache, attn_metadata, residual)
        return hidden_states, residual

    def try_split_forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: torch.Tensor,
        attn_metadata: AttentionMetadata,
        residual: Optional[torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor]:

        def split_tensor(tensor: torch.Tensor,
                         num_partitions: int,
                         dim: int = 0):
            dim_size = tensor.size()[dim] // num_partitions
            tensor_list = torch.split(tensor, dim_size, dim=dim)
            #tensor_list = tuple(chunk.contiguous() for chunk in tensor_list)
            return tensor_list

        split = self.tp_parallel_size
        positions_list = split_tensor(positions, split, dim=0)
        if self.layer_idx == 0:
            hidden_states_list = split_tensor(hidden_states, split, dim=0)
            residual_list = [None
                             ] * split if residual is None else split_tensor(
                                 residual, split, dim=0)
        else:
            hidden_states_list = hidden_states
            residual_list = residual
        res_hidden_states_list = [None] * split
        res_residual_list = [None] * split
        block_indices_list = split_tensor(
            attn_metadata.block_indices, split
        ) if attn_metadata.block_indices is not None else [None] * split
        block_offsets_list = split_tensor(
            attn_metadata.block_offsets, split
        ) if attn_metadata.block_offsets is not None else [None] * split
        seq_lens_tensor_list = split_tensor(
            attn_metadata.seq_lens_tensor, split
        ) if attn_metadata.seq_lens_tensor is not None else [None] * split
        attn_bias_list = split_tensor(
            attn_metadata.attn_bias,
            split) if attn_metadata.attn_bias is not None else [None] * split

        for i in range(split):
            if residual is None:
                res_residual_list[i] = hidden_states_list[i]
                res_hidden_states_list[i] = self.input_layernorm(
                    hidden_states_list[i])
            else:
                res_hidden_states_list[i], res_residual_list[
                    i] = self.input_layernorm(hidden_states_list[i],
                                              residual_list[i])
            res_hidden_states_list[i] = self.self_attn(
                positions=positions_list[i],
                hidden_states=res_hidden_states_list[i],
                kv_cache=kv_cache,
                attn_metadata=attn_metadata,
                block_indices=block_indices_list[i],
                block_offsets=block_offsets_list[i],
                seq_lens_tensor=seq_lens_tensor_list[i],
                attn_bias=attn_bias_list[i])
        for i in range(split):
            res_hidden_states_list[i], res_residual_list[
                i] = self.post_attention_layernorm(res_hidden_states_list[i],
                                                   res_residual_list[i])
            res_hidden_states_list[i] = self.mlp(res_hidden_states_list[i])

        if self.layer_idx == (self.total_num_layers - 1):
            hidden_states = torch.cat(res_hidden_states_list, dim=0)
            residual = torch.cat(res_residual_list, dim=0)
        else:
            hidden_states = res_hidden_states_list
            residual = res_residual_list

        return hidden_states, residual


@support_torch_compile
class LlamaModel(nn.Module):

    def __init__(self,
                 *,
                 vllm_config: VllmConfig,
                 prefix: str = "",
                 layer_type: Type[LlamaDecoderLayer] = LlamaDecoderLayer):
        super().__init__()

        config = vllm_config.model_config.hf_config
        cache_config = vllm_config.cache_config
        quant_config = vllm_config.quant_config
        lora_config = vllm_config.lora_config

        self.config = config
        self.padding_idx = config.pad_token_id
        lora_vocab = (lora_config.lora_extra_vocab_size *
                      (lora_config.max_loras or 1)) if lora_config else 0
        self.vocab_size = config.vocab_size + lora_vocab
        self.org_vocab_size = config.vocab_size
        if get_pp_group().is_first_rank or (config.tie_word_embeddings
                                            and get_pp_group().is_last_rank):
            self.embed_tokens = VocabParallelEmbedding(
                self.vocab_size,
                config.hidden_size,
                org_num_embeddings=config.vocab_size,
                quant_config=quant_config,
            )
        else:
            self.embed_tokens = PPMissingLayer()
        self.start_layer, self.end_layer, self.layers = make_layers(
            config.num_hidden_layers,
            lambda prefix: layer_type(config=config,
                                      cache_config=cache_config,
                                      quant_config=quant_config,
                                      prefix=prefix),
            prefix=f"{prefix}.layers",
        )
        for layer in self.layers:
            layer.set_total_num_layers(len(self.layers))

        if get_pp_group().is_last_rank:
            self.norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        else:
            self.norm = PPMissingLayer()

        self.make_empty_intermediate_tensors = (
            make_empty_intermediate_tensors_factory(
                ["hidden_states", "residual"], config.hidden_size))

        self.split_qk_v = cache_config.split_qk_v
        self.split_gate_up = cache_config.split_gate_up

    def get_input_embeddings(self, input_ids: torch.Tensor) -> torch.Tensor:
        return self.embed_tokens(input_ids)

    def forward(
        self,
        input_ids: Optional[torch.Tensor],
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
        intermediate_tensors: Optional[IntermediateTensors],
        inputs_embeds: Optional[torch.Tensor] = None,
    ) -> Union[torch.Tensor, IntermediateTensors]:
        if get_pp_group().is_first_rank:
            if inputs_embeds is not None:
                hidden_states = inputs_embeds
            else:
                hidden_states = self.get_input_embeddings(input_ids)
            residual = None
        else:
            assert intermediate_tensors is not None
            hidden_states = intermediate_tensors["hidden_states"]
            residual = intermediate_tensors["residual"]

        if is_hpu:
            import habana_frameworks.torch as htorch
            htorch.core.mark_step()

        for i in range(self.start_layer, self.end_layer):
            layer = self.layers[i]
            hidden_states, residual = layer(positions, hidden_states,
                                            kv_caches[i - self.start_layer],
                                            attn_metadata, residual)
        if not get_pp_group().is_last_rank:
            return IntermediateTensors({
                "hidden_states": hidden_states,
                "residual": residual
            })

        hidden_states, _ = self.norm(hidden_states, residual)
        return hidden_states

    def load_weights(self, weights: Iterable[Tuple[str,
                                                   torch.Tensor]]) -> Set[str]:
        stacked_params_mapping = [
            # (param_name, shard_name, shard_id)
            # (".gate_up_proj", ".gate_proj", 0),
            # (".gate_up_proj", ".up_proj", 1),
        ]
        if not self.split_qk_v:
            stacked_params_mapping.append((".qkv_proj", ".q_proj", "q"))
            stacked_params_mapping.append((".qkv_proj", ".k_proj", "k"))
            stacked_params_mapping.append((".qkv_proj", ".v_proj", "v"))

        if not self.split_gate_up:
            stacked_params_mapping.append((".gate_up_proj", ".gate_proj", 0))
            stacked_params_mapping.append((".gate_up_proj", ".up_proj", 1))
        
        params_dict = dict(self.named_parameters())
        loaded_params: Set[str] = set()
        for name, loaded_weight in weights:
            if "rotary_emb.inv_freq" in name:
                continue
            if ("rotary_emb.cos_cached" in name
                    or "rotary_emb.sin_cached" in name):
                # Models trained using ColossalAI may include these tensors in
                # the checkpoint. Skip them.
                continue
            if scale_name := get_compressed_tensors_cache_scale(name):
                # Loading kv cache scales for compressed-tensors quantization
                param = params_dict[scale_name]
                weight_loader = getattr(param, "weight_loader",
                                        default_weight_loader)
                loaded_weight = loaded_weight[0]
                weight_loader(param, loaded_weight)
                loaded_params.add(scale_name)
                continue
            for param_name, weight_name, shard_id in stacked_params_mapping:
                if weight_name not in name:
                    continue
                name = name.replace(weight_name, param_name)
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue

                if is_pp_missing_parameter(name, self):
                    continue

                param = params_dict[name]
                weight_loader = param.weight_loader
                weight_loader(param, loaded_weight, shard_id)
                break
            else:
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue
                # Remapping the name of FP8 kv-scale.
                name = maybe_remap_kv_scale_name(name, params_dict)
                if name is None:
                    continue

                if is_pp_missing_parameter(name, self):
                    continue

                param = params_dict[name]
                weight_loader = getattr(param, "weight_loader",
                                        default_weight_loader)
                weight_loader(param, loaded_weight)
            loaded_params.add(name)
            if is_hpu:
                torch.hpu.synchronize()
        return loaded_params

    # If this function is called, it should always initialize KV cache scale
    # factors (or else raise an exception). Thus, handled exceptions should
    # make sure to leave KV cache scale factors in a known good (dummy) state
    def load_kv_cache_scales(self, quantization_param_path: str) -> None:
        tp_size = get_tensor_model_parallel_world_size()
        tp_rank = get_tensor_model_parallel_rank()
        for layer_idx, scaling_factor in kv_cache_scales_loader(
                quantization_param_path, tp_rank, tp_size,
                self.config.num_hidden_layers,
                self.config.__class__.model_type):
            if not isinstance(self.layers[layer_idx], nn.Identity):
                layer_self_attn = self.layers[layer_idx].self_attn

            if current_platform.is_rocm():
                # The scaling factor convention we are assuming is
                # quantized_value * scaling_factor ~= true_value
                # which is consistent with the practice of setting
                # scaling_factor = tensor_amax / FPtype_max
                scaling_factor *= 2
            if hasattr(layer_self_attn, "kv_scale"):
                layer_self_attn.attn._kv_scale = scaling_factor
            else:
                raise RuntimeError("Self attention has no KV cache scaling "
                                   "factor attribute!")


class LlamaForCausalLM(nn.Module, SupportsLoRA, SupportsPP):
    packed_modules_mapping = {
        "qkv_proj": ["q_proj", "k_proj", "v_proj"],
        "gate_up_proj": ["gate_proj", "up_proj"]
    }

    # LoRA specific attributes
    supported_lora_modules = [
        "qkv_proj", "o_proj", "gate_up_proj", "down_proj", "embed_tokens",
        "lm_head"
    ]
    embedding_modules = {
        "embed_tokens": "input_embeddings",
        "lm_head": "output_embeddings"
    }
    embedding_padding_modules = ["lm_head"]

    # BitandBytes specific attributes
    bitsandbytes_stacked_params_mapping = {
        # shard_name, weight_name, index
        "q_proj": ("qkv_proj", 0),
        "k_proj": ("qkv_proj", 1),
        "v_proj": ("qkv_proj", 2),
        "gate_proj": ("gate_up_proj", 0),
        "up_proj": ("gate_up_proj", 1),
    }

    # Mistral/Llama models can also be loaded with --load-format mistral
    # from consolidated.safetensors checkpoints
    mistral_mapping = {
        "layers": "model.layers",
        "attention": "self_attn",
        "wq": "q_proj",
        "wk": "k_proj",
        "wv": "v_proj",
        "wo": "o_proj",
        "attention_norm": "input_layernorm",
        "feed_forward": "mlp",
        "w1": "gate_proj",
        "w2": "down_proj",
        "w3": "up_proj",
        "ffn_norm": "post_attention_layernorm",
        "tok_embeddings": "model.embed_tokens",
        "output": "lm_head",
        "norm": "model.norm"
    }

    def __init__(self, *, vllm_config: VllmConfig, prefix: str = ""):
        super().__init__()
        config = vllm_config.model_config.hf_config
        quant_config = vllm_config.quant_config
        lora_config = vllm_config.lora_config
        self.config = config
        self.lora_config = lora_config

        self.model = self._init_model(vllm_config=vllm_config,
                                      prefix=maybe_prefix(prefix, "model"))

        if get_pp_group().is_last_rank:
            self.unpadded_vocab_size = config.vocab_size
            if lora_config:
                self.unpadded_vocab_size += lora_config.lora_extra_vocab_size
            self.lm_head = ParallelLMHead(
                self.unpadded_vocab_size,
                config.hidden_size,
                org_num_embeddings=config.vocab_size,
                padding_size=(
                    DEFAULT_VOCAB_PADDING_SIZE
                    # We need bigger padding if using lora for kernel
                    # compatibility
                    if not lora_config else
                    lora_config.lora_vocab_padding_size),
                quant_config=quant_config,
                prefix=maybe_prefix(prefix, "lm_head"),
            )
            if config.tie_word_embeddings:
                self.lm_head = self.lm_head.tie_weights(
                    self.model.embed_tokens)

            logit_scale = getattr(config, "logit_scale", 1.0)
            self.logits_processor = LogitsProcessor(self.unpadded_vocab_size,
                                                    config.vocab_size,
                                                    logit_scale)
        else:
            self.lm_head = PPMissingLayer()

        self.sampler = get_sampler()

        self.make_empty_intermediate_tensors = (
            self.model.make_empty_intermediate_tensors)

    def _init_model(self, vllm_config: VllmConfig, prefix: str = ""):
        return LlamaModel(vllm_config=vllm_config, prefix=prefix)

    def get_input_embeddings(self, input_ids: torch.Tensor) -> torch.Tensor:
        return self.model.get_input_embeddings(input_ids)

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
        intermediate_tensors: Optional[IntermediateTensors] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
    ) -> Union[torch.Tensor, IntermediateTensors]:
        model_output = self.model(input_ids, positions, kv_caches,
                                  attn_metadata, intermediate_tensors,
                                  inputs_embeds)
        return model_output

    def compute_logits(
        self,
        hidden_states: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> Optional[torch.Tensor]:
        logits = self.logits_processor(self.lm_head, hidden_states,
                                       sampling_metadata)
        return logits

    def sample(self, logits: torch.Tensor,
               sampling_metadata: SamplingMetadata) -> Optional[SamplerOutput]:
        next_tokens = self.sampler(logits, sampling_metadata)
        return next_tokens

    def load_weights(self, weights: Iterable[Tuple[str,
                                                   torch.Tensor]]) -> Set[str]:
        loader = AutoWeightsLoader(
            self,
            skip_prefixes=(["lm_head."]
                           if self.config.tie_word_embeddings else None),
        )
        return loader.load_weights(
            self.maybe_remap_mistral(name, loaded_weight)
            for name, loaded_weight in weights)

    def load_kv_cache_scales(self, quantization_param_path: str) -> None:
        self.model.load_kv_cache_scales(quantization_param_path)

    # This function is used to remap the mistral format as
    # used by Mistral and Llama <=2
    def maybe_remap_mistral(
        self,
        name: str,
        loaded_weight: torch.Tensor,
    ) -> Tuple[str, torch.Tensor]:

        def permute(w: torch.Tensor, n_heads: int):
            attn_in = self.config.head_dim * n_heads
            attn_out = self.config.hidden_size

            return w.view(n_heads, attn_in // n_heads // 2, 2,
                          attn_out).transpose(1, 2).reshape(attn_in, attn_out)

        mapping = self.mistral_mapping
        modules = name.split(".")

        # rotary embeds should be sliced
        if "wk" in modules:
            loaded_weight = permute(loaded_weight,
                                    self.config.num_key_value_heads)
        elif "wq" in modules:
            loaded_weight = permute(loaded_weight,
                                    self.config.num_attention_heads)

        for item in modules:
            if item in mapping and mapping[item] not in name:
                name = name.replace(item, mapping[item])

        return name, loaded_weight
