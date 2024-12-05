# Adapted from https://github.com/lucidrains/make-a-video-pytorch/blob/main/make_a_video_pytorch/make_a_video.py
# Borrowed from https://github.com/xuduo35/MakeLongVideo/blob/main/makelongvideo/models/resnet.py

import torch
import torch.nn as nn
import torch.nn.functional as F

from dataclasses import dataclass
from typing import Optional
from diffusers.configuration_utils import ConfigMixin, register_to_config
from diffusers.models.modeling_utils import ModelMixin
from diffusers.utils import BaseOutput
from diffusers.models.attention import BasicTransformerBlock
from torch import nn
    
class caTConvs(nn.Module):
    def __init__(self, dim, kernel_size, num_layers):
        super().__init__()

        self.spatio_temporal_convs = ConvBlocks(dim, num_layers=num_layers, kernel_size=kernel_size, dropout=0.1)

    def forward(self, hidden_states, num_frames):
        hidden_states = (
            hidden_states[None, :].reshape((-1, num_frames) + hidden_states.shape[1:]).permute(0, 2, 1, 3, 4)
        )

        hidden_states = self.spatio_temporal_convs(hidden_states)

        hidden_states = hidden_states.permute(0, 2, 1, 3, 4).reshape(
            (hidden_states.shape[0] * hidden_states.shape[2], -1) + hidden_states.shape[3:]
        )

        return hidden_states
    
class ConvBlocks(nn.Module):
    def __init__(
        self,
        dim: int,
        num_layers: int = 1,
        kernel_size: int = 3,
        dropout: float = 0.1,
        norm_num_groups: int = 32,
    ):
        super().__init__()
        self.dim = dim
        self.num_layers = num_layers
        self.kernel_size = kernel_size
        self.padding = (self.kernel_size - 1) // 2

        self.convs = nn.ModuleList()
        for i in range(num_layers):
            layer = nn.Sequential(
                nn.GroupNorm(min(norm_num_groups, dim), dim),
                nn.SiLU(),
                nn.Dropout(dropout) if i > 0 else nn.Identity(), 
                nn.Conv3d(dim, dim, (self.kernel_size, self.kernel_size, self.kernel_size), padding=(self.padding, self.padding, self.padding))
            )
            self.convs.append(layer)

        #nn.init.zeros_(self.convs[-1][-1].weight)
        #nn.init.zeros_(self.convs[-1][-1].bias)

    def forward(self, hidden_states):
        residual = hidden_states

        for conv in self.convs:
            hidden_states = conv(hidden_states)

        hidden_states = residual + hidden_states
        return hidden_states
 
class BiDirectionalExponentialEncodings(nn.Module):
    def __init__(self):
        super(BiDirectionalExponentialEncodings, self).__init__()

    def _get_embeddings(self, seq_len, d_model, reverse=False):
        pe = torch.zeros(seq_len, d_model)
        position = torch.arange(0, seq_len, dtype=torch.float).unsqueeze(1)
        
        if reverse:
            weights = torch.exp(-position / seq_len)
        else:
            weights = torch.exp(position / seq_len)

        for i in range(d_model):
            pe[:, i] = weights.squeeze()
        
        return pe.unsqueeze(0)

    def forward(self, x, reverse=False):
        _, seq_len, d_model = x.shape
        pe = self._get_embeddings(seq_len, d_model, reverse)
        return pe.to(x.device)
    
@dataclass
class caTConditioningTransformerOutput(BaseOutput):
    sample: torch.FloatTensor

class caTConditioningTransformerModel(ModelMixin, ConfigMixin):
    @register_to_config
    def __init__(
        self,
        num_attention_heads: int = 16,
        attention_head_dim: int = 88,
        in_channels: Optional[int] = None,
        cross_attention_dim: int = 1280,
        num_layers: int = 1,
        only_cross_attention: bool = True,
        dropout: float = 0.0,
        attention_bias: bool = False,
        activation_fn: str = "geglu",
        norm_elementwise_affine: bool = True,
    ):
        super().__init__()

        self.encoder_conv_in = nn.Conv3d(4, cross_attention_dim, kernel_size=(1, 1, 1))

        self.hidden_ln = nn.LayerNorm(in_channels)
        self.hidden_proj_in = nn.Linear(in_channels, cross_attention_dim)

        self.positional_encoding = BiDirectionalExponentialEncodings()

        self.transformer_blocks = nn.ModuleList(
            [
                BasicTransformerBlock(
                    cross_attention_dim,
                    num_attention_heads,
                    attention_head_dim,
                    dropout=dropout,
                    cross_attention_dim=cross_attention_dim,
                    activation_fn=activation_fn,
                    attention_bias=attention_bias,
                    only_cross_attention=only_cross_attention,
                    norm_elementwise_affine=norm_elementwise_affine
                )
                for _ in range(num_layers)
            ]
        )

        self.hidden_proj_out = nn.Linear(cross_attention_dim, in_channels)

    def forward(
        self,
        hidden_states,
        encoder_hidden_states,
        num_frames,
        return_dict: bool = True,       
    ):
        if encoder_hidden_states is None:
            if not return_dict:
                return (hidden_states,)

            return caTConditioningTransformerOutput(sample=hidden_states)
            
        if hidden_states.size(2) <= 1:
            if not return_dict:
                return (hidden_states,)

            return caTConditioningTransformerOutput(sample=hidden_states)
        
        hidden_states = (
            hidden_states[None, :].reshape((-1, num_frames) + hidden_states.shape[1:]).permute(0, 2, 1, 3, 4)
        )
        
        h_b, h_c, h_f, h_h, h_w = hidden_states.shape

        residual = hidden_states

        encoder_hidden_states = torch.nn.functional.interpolate(encoder_hidden_states, size=(encoder_hidden_states.shape[2], h_h, h_w), mode='trilinear', align_corners=False)
        if encoder_hidden_states.shape[0] < h_b:
            encoder_hidden_states = encoder_hidden_states.repeat_interleave(repeats=h_b, dim=0)
        encoder_hidden_states = self.encoder_conv_in(encoder_hidden_states)

        e_b, e_c, e_f, e_h, e_w = encoder_hidden_states.shape

        encoder_hidden_states = encoder_hidden_states.permute(0, 3, 4, 2, 1)
        encoder_hidden_states = encoder_hidden_states.reshape(e_b * e_h * e_w, e_f, e_c)
        
        encoder_hidden_states = encoder_hidden_states + self.positional_encoding(encoder_hidden_states, reverse=False)

        hidden_states = hidden_states.permute(0, 3, 4, 2, 1)
        hidden_states = hidden_states.reshape(h_b * h_h * h_w, h_f, h_c)

        hidden_states = self.hidden_ln(hidden_states)
        hidden_states = self.hidden_proj_in(hidden_states)

        hidden_states = hidden_states + self.positional_encoding(hidden_states, reverse=True)

        for block in self.transformer_blocks:
            hidden_states = block(
                hidden_states=hidden_states,
                encoder_hidden_states=encoder_hidden_states
            )

        hidden_states = self.hidden_proj_out(hidden_states)

        hidden_states = hidden_states.view(h_b, h_h, h_w, h_f, h_c).contiguous()
        hidden_states = hidden_states.permute(0, 4, 3, 1, 2)

        hidden_states += residual

        hidden_states = hidden_states.permute(0, 2, 1, 3, 4).reshape(
            (hidden_states.shape[0] * hidden_states.shape[2], -1) + hidden_states.shape[3:]
        )
        
        output = hidden_states

        if not return_dict:
            return (output,)

        return caTConditioningTransformerOutput(sample=output)
