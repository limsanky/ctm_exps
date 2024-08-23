# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# This work is licensed under a Creative Commons
# Attribution-NonCommercial-ShareAlike 4.0 International License.
# You should have received a copy of the license along with this
# work. If not, see http://creativecommons.org/licenses/by-nc-sa/4.0/

"""Model architectures and preconditioning schemes used in the paper
"Elucidating the Design Space of Diffusion-Based Generative Models"."""

import numpy as np
import torch
from torch_utils_cm import persistence
from torch.nn.functional import silu
from .nn import append_dims
import cm.dist_util as dist_util

# from .fp16_util import convert_module_to_f16
# from .bf16_util import convert_module_to_bf16, convert_module_to_f32

#----------------------------------------------------------------------------
# Unified routine for initializing weights and biases.

def weight_init(shape, mode, fan_in, fan_out):
    if mode == 'xavier_uniform': return np.sqrt(6 / (fan_in + fan_out)) * (torch.rand(*shape) * 2 - 1)
    if mode == 'xavier_normal':  return np.sqrt(2 / (fan_in + fan_out)) * torch.randn(*shape)
    if mode == 'kaiming_uniform': return np.sqrt(3 / fan_in) * (torch.rand(*shape) * 2 - 1)
    if mode == 'kaiming_normal':  return np.sqrt(1 / fan_in) * torch.randn(*shape)
    raise ValueError(f'Invalid init mode "{mode}"')

#----------------------------------------------------------------------------
# Fully-connected layer.

@persistence.persistent_class
class Linear(torch.nn.Module):
    def __init__(self, in_features, out_features, bias=True, init_mode='kaiming_normal', init_weight=1, init_bias=0):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        init_kwargs = dict(mode=init_mode, fan_in=in_features, fan_out=out_features)
        self.weight = torch.nn.Parameter(weight_init([out_features, in_features], **init_kwargs) * init_weight)
        self.bias = torch.nn.Parameter(weight_init([out_features], **init_kwargs) * init_bias) if bias else None

    def forward(self, x):
        x = x @ self.weight.to(x.dtype).t()
        if self.bias is not None:
            x = x.add_(self.bias.to(x.dtype))
        return x

#----------------------------------------------------------------------------
# Convolutional layer with optional up/downsampling.

@persistence.persistent_class
class Conv2d(torch.nn.Module):
    def __init__(self,
        in_channels, out_channels, kernel, bias=True, up=False, down=False,
        resample_filter=[1,1], fused_resample=False, init_mode='kaiming_normal', init_weight=1, init_bias=0,
    ):
        assert not (up and down)
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.up = up
        self.down = down
        self.fused_resample = fused_resample
        init_kwargs = dict(mode=init_mode, fan_in=in_channels*kernel*kernel, fan_out=out_channels*kernel*kernel)
        self.weight = torch.nn.Parameter(weight_init([out_channels, in_channels, kernel, kernel], **init_kwargs) * init_weight) if kernel else None
        self.bias = torch.nn.Parameter(weight_init([out_channels], **init_kwargs) * init_bias) if kernel and bias else None
        f = torch.as_tensor(resample_filter, dtype=torch.float32)
        f = f.ger(f).unsqueeze(0).unsqueeze(1) / f.sum().square()
        self.register_buffer('resample_filter', f if up or down else None)

    def forward(self, x):
        w = self.weight.to(x.dtype) if self.weight is not None else None
        b = self.bias.to(x.dtype) if self.bias is not None else None
        f = self.resample_filter.to(x.dtype) if self.resample_filter is not None else None
        w_pad = w.shape[-1] // 2 if w is not None else 0
        f_pad = (f.shape[-1] - 1) // 2 if f is not None else 0

        if self.fused_resample and self.up and w is not None:
            x = torch.nn.functional.conv_transpose2d(x, f.mul(4).tile([self.in_channels, 1, 1, 1]), groups=self.in_channels, stride=2, padding=max(f_pad - w_pad, 0))
            x = torch.nn.functional.conv2d(x, w, padding=max(w_pad - f_pad, 0))
        elif self.fused_resample and self.down and w is not None:
            x = torch.nn.functional.conv2d(x, w, padding=w_pad+f_pad)
            x = torch.nn.functional.conv2d(x, f.tile([self.out_channels, 1, 1, 1]), groups=self.out_channels, stride=2)
        else:
            if self.up:
                x = torch.nn.functional.conv_transpose2d(x, f.mul(4).tile([self.in_channels, 1, 1, 1]), groups=self.in_channels, stride=2, padding=f_pad)
            if self.down:
                x = torch.nn.functional.conv2d(x, f.tile([self.in_channels, 1, 1, 1]), groups=self.in_channels, stride=2, padding=f_pad)
            if w is not None:
                x = torch.nn.functional.conv2d(x, w, padding=w_pad)
        if b is not None:
            x = x.add_(b.reshape(1, -1, 1, 1))
        return x

#----------------------------------------------------------------------------
# Group normalization.

@persistence.persistent_class
class GroupNorm(torch.nn.Module):
    def __init__(self, num_channels, num_groups=32, min_channels_per_group=4, eps=1e-5):
        super().__init__()
        self.num_groups = min(num_groups, num_channels // min_channels_per_group)
        self.eps = eps
        self.weight = torch.nn.Parameter(torch.ones(num_channels))
        self.bias = torch.nn.Parameter(torch.zeros(num_channels))

    def forward(self, x):
        x = torch.nn.functional.group_norm(x, num_groups=self.num_groups, weight=self.weight.to(x.dtype), bias=self.bias.to(x.dtype), eps=self.eps)
        return x

#----------------------------------------------------------------------------
# Attention weight computation, i.e., softmax(Q^T * K).
# Performs all computation using FP32, but uses the original datatype for
# inputs/outputs/gradients to conserve memory.

class AttentionOp(torch.autograd.Function):
    @staticmethod
    def forward(ctx, q, k):
        w = torch.einsum('ncq,nck->nqk', q.to(torch.float32), (k / np.sqrt(k.shape[1])).to(torch.float32)).softmax(dim=2).to(q.dtype).contiguous()
        ctx.save_for_backward(q, k, w)
        return w

    @staticmethod
    def backward(ctx, dw):
        q, k, w = ctx.saved_tensors
        db = torch._softmax_backward_data(grad_output=dw.to(torch.float32), output=w.to(torch.float32), dim=2, input_dtype=torch.float32)
        dq = torch.einsum('nck,nqk->ncq', k.to(torch.float32), db).to(q.dtype).contiguous() / np.sqrt(k.shape[1])
        dk = torch.einsum('ncq,nqk->nck', q.to(torch.float32), db).to(k.dtype).contiguous() / np.sqrt(k.shape[1])
        return dq, dk

#----------------------------------------------------------------------------
# Unified U-Net block with optional up/downsampling and self-attention.
# Represents the union of all features employed by the DDPM++, NCSN++, and
# ADM architectures.

@persistence.persistent_class
class UNetBlock(torch.nn.Module):
    def __init__(self,
        in_channels, out_channels, emb_channels, up=False, down=False, attention=False,
        num_heads=None, channels_per_head=64, dropout=0, skip_scale=1, eps=1e-5,
        resample_filter=[1,1], resample_proj=False, adaptive_scale=True,
        init=dict(), init_zero=dict(init_weight=0), init_attn=None,
        training_mode='', linear_probing=False,
    ):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.emb_channels = emb_channels
        self.num_heads = 0 if not attention else num_heads if num_heads is not None else out_channels // channels_per_head
        self.dropout = dropout
        self.skip_scale = skip_scale
        self.adaptive_scale = adaptive_scale
        self.training_mode = training_mode
        self.linear_probing = linear_probing

        self.norm0 = GroupNorm(num_channels=in_channels, eps=eps)
        self.conv0 = Conv2d(in_channels=in_channels, out_channels=out_channels, kernel=3, up=up, down=down, resample_filter=resample_filter, **init)
        self.affine = Linear(in_features=emb_channels, out_features=out_channels*(2 if adaptive_scale else 1), **init)
        if self.training_mode == 'ctm':
            self.affine_s = Linear(in_features=emb_channels, out_features=out_channels*(2 if adaptive_scale else 1), **init)
        self.norm1 = GroupNorm(num_channels=out_channels, eps=eps)
        self.conv1 = Conv2d(in_channels=out_channels, out_channels=out_channels, kernel=3, **init_zero)

        self.skip = None
        if out_channels != in_channels or up or down:
            kernel = 1 if resample_proj or out_channels!= in_channels else 0
            self.skip = Conv2d(in_channels=in_channels, out_channels=out_channels, kernel=kernel, up=up, down=down, resample_filter=resample_filter, **init)

        if self.num_heads:
            self.norm2 = GroupNorm(num_channels=out_channels, eps=eps)
            self.qkv = Conv2d(in_channels=out_channels, out_channels=out_channels*3, kernel=1, **(init_attn if init_attn is not None else init))
            self.proj = Conv2d(in_channels=out_channels, out_channels=out_channels, kernel=1, **init_zero)

        if linear_probing:
            self.norm0_train = GroupNorm(num_channels=in_channels, eps=eps)
            self.conv0_train = Conv2d(in_channels=in_channels, out_channels=out_channels, kernel=3, up=up, down=down,
                                      resample_filter=resample_filter, **init)
            self.norm1_train = GroupNorm(num_channels=out_channels, eps=eps)
            self.conv1_train = Conv2d(in_channels=out_channels, out_channels=out_channels, kernel=3, **init_zero)

            self.skip_train = None
            if out_channels != in_channels or up or down:
                kernel = 1 if resample_proj or out_channels != in_channels else 0
                self.skip_train = Conv2d(in_channels=in_channels, out_channels=out_channels, kernel=kernel, up=up, down=down,
                                   resample_filter=resample_filter, **init)

            if self.num_heads:
                self.norm2_train = GroupNorm(num_channels=out_channels, eps=eps)
                self.qkv_train = Conv2d(in_channels=out_channels, out_channels=out_channels * 3, kernel=1,
                                  **(init_attn if init_attn is not None else init))
                self.proj_train = Conv2d(in_channels=out_channels, out_channels=out_channels, kernel=1, **init_zero)

    def forward(self, x, emb, emb_s=None, emb_t=None):
        orig = x
        x = self.conv0(silu(self.norm0(x)))

        params = self.affine(emb).unsqueeze(2).unsqueeze(3).to(x.dtype)
        if self.training_mode == 'ctm':
            params_s = self.affine_s(emb_s).unsqueeze(2).unsqueeze(3).to(x.dtype)
            if not self.linear_probing:
                params = params + params_s
        if self.adaptive_scale:
            scale, shift = params.chunk(chunks=2, dim=1)
            x = silu(torch.addcmul(shift, self.norm1(x), scale + 1))
        else:
            x = silu(self.norm1(x.add_(params)))

        x = self.conv1(torch.nn.functional.dropout(x, p=self.dropout, training=self.training))
        x = x.add_(self.skip(orig) if self.skip is not None else orig)
        x = x * self.skip_scale

        if self.num_heads:
            q, k, v = self.qkv(self.norm2(x)).reshape(x.shape[0] * self.num_heads, x.shape[1] // self.num_heads, 3, -1).unbind(2)
            w = AttentionOp.apply(q, k)
            a = torch.einsum('nqk,nck->ncq', w, v).contiguous()
            x = self.proj(a.reshape(*x.shape)).add_(x)
            x = x * self.skip_scale

        if self.linear_probing:
            y = self.conv0_train(silu(self.norm0_train(orig)))
            assert emb_t != None and self.training_mode == 'ctm'
            params_t = self.affine_s(emb_t).unsqueeze(2).unsqueeze(3).to(y.dtype)
            params = params_t - params_s
            params_ = params + params_s
            if self.adaptive_scale:
                scale, shift = params_.chunk(chunks=2, dim=1)
                y = silu(torch.addcmul(shift, self.norm1_train(y), scale + 1))
            else:
                y = silu(self.norm1_train(y.add_(params_)))

            y = self.conv1_train(torch.nn.functional.dropout(y, p=self.dropout, training=self.training))
            y = y.add_(self.skip_train(orig) if self.skip_train is not None else orig)
            y = y * self.skip_scale

            if self.num_heads:
                q, k, v = self.qkv_train(self.norm2_train(y)).reshape(y.shape[0] * self.num_heads, y.shape[1] // self.num_heads, 3,
                                                          -1).unbind(2)
                w = AttentionOp.apply(q, k)
                a = torch.einsum('nqk,nck->ncq', w, v).contiguous()
                y = self.proj_train(a.reshape(*y.shape)).add_(y)
                y = y * self.skip_scale
            y = y.mul(params)

            return x + y
        return x

#----------------------------------------------------------------------------
# Timestep embedding used in the DDPM++ and ADM architectures.

@persistence.persistent_class
class PositionalEmbedding(torch.nn.Module):
    def __init__(self, num_channels, max_positions=10000, endpoint=False):
        super().__init__()
        self.num_channels = num_channels
        self.max_positions = max_positions
        self.endpoint = endpoint

    def forward(self, x):
        freqs = torch.arange(start=0, end=self.num_channels//2, dtype=torch.float32, device=x.device)
        freqs = freqs / (self.num_channels // 2 - (1 if self.endpoint else 0))
        freqs = (1 / self.max_positions) ** freqs
        x = x.ger(freqs.to(x.dtype))
        x = torch.cat([x.cos(), x.sin()], dim=1)
        return x

#----------------------------------------------------------------------------
# Timestep embedding used in the NCSN++ architecture.

@persistence.persistent_class
class FourierEmbedding(torch.nn.Module):
    # def __init__(self, num_channels, scale=16):
    def __init__(self, num_channels, scale=0.02):
        super().__init__()
        self.register_buffer('freqs', torch.randn(num_channels // 2) * scale)

    def forward(self, x):
        x = x.ger((2 * np.pi * self.freqs).to(x.dtype))
        x = torch.cat([x.cos(), x.sin()], dim=1)
        return x

#----------------------------------------------------------------------------
# Reimplementation of the DDPM++ and NCSN++ architectures from the paper
# "Score-Based Generative Modeling through Stochastic Differential
# Equations". Equivalent to the original implementation by Song et al.,
# available at https://github.com/yang-song/score_sde_pytorch

@persistence.persistent_class
class SongUNet(torch.nn.Module):
    def __init__(self,
        img_resolution,                     # Image resolution at input/output.
        in_channels,                        # Number of color channels at input.
        out_channels,                       # Number of color channels at output.
        label_dim           = 0,            # Number of class labels, 0 = unconditional.
        augment_dim         = 0,            # Augmentation label dimensionality, 0 = no augmentation.

        model_channels      = 128,          # Base multiplier for the number of channels.
        channel_mult        = [2,2,2],    # Per-resolution multipliers for the number of channels.
        channel_mult_emb    = 4,            # Multiplier for the dimensionality of the embedding vector.
        num_blocks          = 4,            # Number of residual blocks per resolution.
        attn_resolutions    = [16],         # List of resolutions with self-attention.
        dropout             = 0.13,         # Dropout probability of intermediate activations.
        label_dropout       = 0,            # Dropout probability of class labels for classifier-free guidance.

        embedding_type      = 'fourier', # Timestep embedding type: 'positional' for DDPM++, 'fourier' for NCSN++.
        channel_mult_noise  = 2,            # Timestep embedding size: 1 for DDPM++, 2 for NCSN++.
        encoder_type        = 'residual',   # Encoder architecture: 'standard' for DDPM++, 'residual' for NCSN++.
        decoder_type        = 'standard',   # Decoder architecture: 'standard' for both DDPM++ and NCSN++.
        resample_filter     = [1,3,3,1],        # Resampling filter: [1,1] for DDPM++, [1,3,3,1] for NCSN++.
        training_mode = '',
        linear_probing=False,
        
        condition_mode      = None,
    ):
        assert embedding_type in ['fourier', 'positional']
        assert encoder_type in ['standard', 'skip', 'residual']
        assert decoder_type in ['standard', 'skip']
        self.training_mode = training_mode
        self.linear_probing = linear_probing
        self.img_resolution = img_resolution

        super().__init__()
                
        self.condition_mode = condition_mode
        in_channels = 2 * in_channels if self.condition_mode == 'concat' else in_channels
        
        self.label_dropout = label_dropout
        emb_channels = model_channels * channel_mult_emb
        noise_channels = model_channels * channel_mult_noise
        init = dict(init_mode='xavier_uniform')
        init_zero = dict(init_mode='xavier_uniform', init_weight=1e-5)
        init_attn = dict(init_mode='xavier_uniform', init_weight=np.sqrt(0.2))
        block_kwargs = dict(
            emb_channels=emb_channels, num_heads=1, dropout=dropout, skip_scale=np.sqrt(0.5), eps=1e-6,
            resample_filter=resample_filter, resample_proj=True, adaptive_scale=False,
            init=init, init_zero=init_zero, init_attn=init_attn,
        )

        # Mapping.
        self.map_noise = PositionalEmbedding(num_channels=noise_channels, endpoint=True) if embedding_type == 'positional' else FourierEmbedding(num_channels=noise_channels)
        self.map_label = Linear(in_features=label_dim, out_features=noise_channels, **init) if label_dim else None
        self.map_augment = Linear(in_features=augment_dim, out_features=noise_channels, bias=False, **init) if augment_dim else None
        self.map_layer0 = Linear(in_features=noise_channels, out_features=emb_channels, **init)
        self.map_layer1 = Linear(in_features=emb_channels, out_features=emb_channels, **init)
        if self.training_mode.lower() == 'ctm':
            self.map_layer0_s = Linear(in_features=noise_channels, out_features=emb_channels, **init)
            self.map_layer1_s = Linear(in_features=emb_channels, out_features=emb_channels, **init)

        # Encoder.
        self.enc = torch.nn.ModuleDict()
        cout = in_channels
        caux = in_channels
        for level, mult in enumerate(channel_mult):
            res = img_resolution >> level
            if level == 0:
                cin = cout
                cout = model_channels
                self.enc[f'{res}x{res}_conv'] = Conv2d(in_channels=cin, out_channels=cout, kernel=3, **init)
            else:
                self.enc[f'{res}x{res}_down'] = UNetBlock(in_channels=cout, out_channels=cout, down=True,
                                                          training_mode=training_mode, linear_probing=linear_probing, **block_kwargs)
                if encoder_type == 'skip':
                    self.enc[f'{res}x{res}_aux_down'] = Conv2d(in_channels=caux, out_channels=caux, kernel=0, down=True, resample_filter=resample_filter)
                    self.enc[f'{res}x{res}_aux_skip'] = Conv2d(in_channels=caux, out_channels=cout, kernel=1, **init)
                if encoder_type == 'residual':
                    self.enc[f'{res}x{res}_aux_residual'] = Conv2d(in_channels=caux, out_channels=cout, kernel=3, down=True, resample_filter=resample_filter, fused_resample=True, **init)
                    caux = cout
            for idx in range(num_blocks):
                cin = cout
                cout = model_channels * mult
                attn = (res in attn_resolutions)
                self.enc[f'{res}x{res}_block{idx}'] = UNetBlock(in_channels=cin, out_channels=cout, attention=attn,
                                                                training_mode=training_mode, linear_probing=linear_probing, **block_kwargs)
        skips = [block.out_channels for name, block in self.enc.items() if 'aux' not in name]

        # Decoder.
        self.dec = torch.nn.ModuleDict()
        for level, mult in reversed(list(enumerate(channel_mult))):
            res = img_resolution >> level
            if level == len(channel_mult) - 1:
                self.dec[f'{res}x{res}_in0'] = UNetBlock(in_channels=cout, out_channels=cout, attention=True,
                                                         training_mode=training_mode, linear_probing=linear_probing, **block_kwargs)
                self.dec[f'{res}x{res}_in1'] = UNetBlock(in_channels=cout, out_channels=cout,
                                                         training_mode=training_mode, linear_probing=linear_probing, **block_kwargs)
            else:
                self.dec[f'{res}x{res}_up'] = UNetBlock(in_channels=cout, out_channels=cout, up=True,
                                                        training_mode=training_mode, linear_probing=linear_probing, **block_kwargs)
            for idx in range(num_blocks + 1):
                cin = cout + skips.pop()
                cout = model_channels * mult
                attn = (idx == num_blocks and res in attn_resolutions)
                self.dec[f'{res}x{res}_block{idx}'] = UNetBlock(in_channels=cin, out_channels=cout, attention=attn,
                                                                training_mode=training_mode, linear_probing=linear_probing, **block_kwargs)
            if decoder_type == 'skip' or level == 0:
                if decoder_type == 'skip' and level < len(channel_mult) - 1:
                    self.dec[f'{res}x{res}_aux_up'] = Conv2d(in_channels=out_channels, out_channels=out_channels, kernel=0, up=True, resample_filter=resample_filter)
                self.dec[f'{res}x{res}_aux_norm'] = GroupNorm(num_channels=cout, eps=1e-6)
                self.dec[f'{res}x{res}_aux_conv'] = Conv2d(in_channels=cout, out_channels=out_channels, kernel=3, **init_zero)
                if self.linear_probing:
                    self.dec[f'{res}x{res}_aux_norm_train'] = GroupNorm(num_channels=cout, eps=1e-6)
                    self.dec[f'{res}x{res}_aux_lin_train'] = Linear(in_features=emb_channels, out_features=out_channels*img_resolution*img_resolution, **init)
                    self.dec[f'{res}x{res}_aux_conv_train'] = Conv2d(in_channels=cout, out_channels=out_channels, kernel=3,
                                                               **init_zero)

    def convert_to_fp16(self):
        """
        Convert the torso of the model to float16.
        """
        
        # self.map_label.apply(convert_module_to_f16)
        # self.map_augment.apply(convert_module_to_f16)
        # self.map_layer0.apply(convert_module_to_f16)
        # self.map_layer1.apply(convert_module_to_f16)
        
        # if self.training_mode.lower() == 'ctm':
        #     self.map_layer0_s.apply(convert_module_to_f16)
        #     self.map_layer1_s.apply(convert_module_to_f16)

        def _helper_convert_module_to_f16(l):
            if isinstance(l, Conv2d):
                l.weight.data = l.weight.data.half()
                if l.bias is not None:
                    l.bias.data = l.bias.data.half()
            elif isinstance(l, (torch.nn.Conv1d, torch.nn.Conv2d, torch.nn.Conv3d)):
                print('error!')
                exit()
                l.weight.data = l.weight.data.half()
                if l.bias is not None:
                    l.bias.data = l.bias.data.half()
            elif isinstance(l, UNetBlock):
                for name, m in l.named_children():
                    if isinstance(m, (torch.nn.Conv1d, torch.nn.Conv2d, torch.nn.Conv3d)):
                        print('error!')
                        exit()
                    elif isinstance(m, Conv2d):
                        m.weight.data = m.weight.data.half()
                        if m.bias is not None:
                            m.bias.data = m.bias.data.half()
        
        self.enc.apply(_helper_convert_module_to_f16)
        self.dec.apply(_helper_convert_module_to_f16)

        # self.input_blocks.apply(convert_module_to_f16)
        # self.middle_block.apply(convert_module_to_f16)
        # self.output_blocks.apply(convert_module_to_f16)
    
    def convert_to_bf16(self):
        """
        Convert the torso of the model to bfloat16.
        """
        # self.map_label.apply(convert_module_to_bf16)
        # self.map_augment.apply(convert_module_to_bf16)
        # self.map_layer0.apply(convert_module_to_bf16)
        # self.map_layer1.apply(convert_module_to_bf16)
        
        # if self.training_mode.lower() == 'ctm':
        #     self.map_layer0_s.apply(convert_module_to_bf16)
        #     self.map_layer1_s.apply(convert_module_to_bf16)
    
        def _helper_convert_module_to_bf16(l):
            if isinstance(l, Conv2d):
                l.weight.data = l.weight.data.bfloat16()
                if l.bias is not None:
                    l.bias.data = l.bias.data.bfloat16()
            elif isinstance(l, (torch.nn.Conv1d, torch.nn.Conv2d, torch.nn.Conv3d)):
                print('error!')
                exit()
                l.weight.data = l.weight.data.bfloat16()
                if l.bias is not None:
                    l.bias.data = l.bias.data.bfloat16()
            elif isinstance(l, UNetBlock):
                for name, m in l.named_children():
                    if isinstance(m, (torch.nn.Conv1d, torch.nn.Conv2d, torch.nn.Conv3d)):
                        print('error!')
                        exit()
                    elif isinstance(m, Conv2d):
                        m.weight.data = m.weight.data.bfloat16()
                        if m.bias is not None:
                            m.bias.data = m.bias.data.bfloat16()

        self.enc.apply(_helper_convert_module_to_bf16)
        self.dec.apply(_helper_convert_module_to_bf16)
        
        # self.input_blocks.apply(convert_module_to_bf16)
        # self.middle_block.apply(convert_module_to_bf16)
        # self.output_blocks.apply(convert_module_to_bf16)

    def convert_to_fp32(self):
        """
        Convert the torso of the model to float32.
        """
        # self.map_label.apply(convert_module_to_f32)
        # self.map_augment.apply(convert_module_to_f32)
        # self.map_layer0.apply(convert_module_to_f32)
        # self.map_layer1.apply(convert_module_to_f32)
        
        # if self.training_mode.lower() == 'ctm':
        #     self.map_layer0_s.apply(convert_module_to_f32)
        #     self.map_layer1_s.apply(convert_module_to_f32)

        def _helper_convert_module_to_f32(l):
            if isinstance(l, Conv2d):
                l.weight.data = l.weight.data.float()
                if l.bias is not None:
                    l.bias.data = l.bias.data.float()
            elif isinstance(l, (torch.nn.Conv1d, torch.nn.Conv2d, torch.nn.Conv3d)):
                l.weight.data = l.weight.data.float()
                if l.bias is not None:
                    l.bias.data = l.bias.data.float()
            elif isinstance(l, UNetBlock):
                for name, m in l.named_children():
                    if isinstance(m, (torch.nn.Conv1d, torch.nn.Conv2d, torch.nn.Conv3d)):
                        print('error!')
                        exit()
                    elif isinstance(m, Conv2d):
                        m.weight.data = m.weight.data.float()
                        if m.bias is not None:
                            m.bias.data = m.bias.data.float()
        self.enc.apply(_helper_convert_module_to_f32)
        self.dec.apply(_helper_convert_module_to_f32)

        # self.input_blocks.apply(convert_module_to_f32)
        # self.middle_block.apply(convert_module_to_f32)
        # self.output_blocks.apply(convert_module_to_f32)
    
    def forward(self, x, noise_labels, noise_labels_s, class_labels, x_T=None):
        
        if self.condition_mode == 'concat': # Should be true only for e2s and e2h and diode datasets (probably)!!
            # print(xT.shape)
            # exit()
            assert x_T is not None
            x = torch.cat([x, x_T], dim=1)
        
        # Mapping.
        emb = self.map_noise(noise_labels)
        emb = emb.reshape(emb.shape[0], 2, -1).flip(1).reshape(*emb.shape) # swap sin/cos
        if self.map_label is not None:
            tmp = class_labels
            if self.training and self.label_dropout:
                tmp = tmp * (torch.rand([x.shape[0], 1], device=x.device) >= self.label_dropout).to(tmp.dtype)
            emb = emb + self.map_label(tmp * np.sqrt(self.map_label.in_features))
        emb = silu(self.map_layer0(emb))
        emb = silu(self.map_layer1(emb))
        
        if noise_labels_s != None:
            emb_s = self.map_noise(noise_labels_s)
            emb_s = emb_s.reshape(emb_s.shape[0], 2, -1).flip(1).reshape(*emb_s.shape)  # swap sin/cos
            if self.map_label is not None:
                tmp = class_labels
                if self.training and self.label_dropout:
                    tmp = tmp * (torch.rand([x.shape[0], 1], device=x.device) >= self.label_dropout).to(tmp.dtype)
                emb_s = emb_s + self.map_label(tmp * np.sqrt(self.map_label.in_features))
            emb_s = silu(self.map_layer0_s(emb_s))
            emb_s = silu(self.map_layer1_s(emb_s))
            if self.linear_probing:
                emb_t = self.map_noise(noise_labels)
                emb_t = emb_t.reshape(emb_t.shape[0], 2, -1).flip(1).reshape(*emb_t.shape)  # swap sin/cos
                emb_t = silu(self.map_layer0_s(emb_t))
                emb_t = silu(self.map_layer1_s(emb_t))

        # Encoder.
        skips = []
        aux = x
        for name, block in self.enc.items():
            if 'aux_down' in name:
                aux = block(aux)
            elif 'aux_skip' in name:
                x = skips[-1] = x + block(aux)
            elif 'aux_residual' in name:
                x = skips[-1] = aux = (x + block(aux)) / np.sqrt(2)
            else:
                x = block(x, emb, emb_s=None if noise_labels_s == None else emb_s,
                          emb_t=emb_t if self.linear_probing else None) if isinstance(block, UNetBlock) else block(x)
                skips.append(x)

        # Decoder.
        aux = None
        tmp = None
        for name, block in self.dec.items():
            if 'aux_up' in name:
                aux = block(aux)
            elif 'aux_norm' in name:
                tmp = block(x)
            elif 'aux_lin' in name:
                emb_mult = (block(emb_t) - block(emb_s)).reshape(-1, 3, self.img_resolution, self.img_resolution)
            elif 'aux_conv' in name:
                tmp = block(silu(tmp))
                aux = tmp if aux is None else tmp * emb_mult + aux
            else:
                if x.shape[1] != block.in_channels:
                    x = torch.cat([x, skips.pop()], dim=1)
                x = block(x, emb, emb_s=None if noise_labels_s == None else emb_s,
                          emb_t=emb_t if self.linear_probing else None)
        return aux

#----------------------------------------------------------------------------
# Reimplementation of the ADM architecture from the paper
# "Diffusion Models Beat GANS on Image Synthesis". Equivalent to the
# original implementation by Dhariwal and Nichol, available at
# https://github.com/openai/guided-diffusion

@persistence.persistent_class
class DhariwalUNet(torch.nn.Module):
    def __init__(self,
        img_resolution,                     # Image resolution at input/output.
        in_channels,                        # Number of color channels at input.
        out_channels,                       # Number of color channels at output.
        label_dim           = 0,            # Number of class labels, 0 = unconditional.
        augment_dim         = 0,            # Augmentation label dimensionality, 0 = no augmentation.

        model_channels      = 192,          # Base multiplier for the number of channels.
        channel_mult        = [1,2,3,4],    # Per-resolution multipliers for the number of channels.
        channel_mult_emb    = 4,            # Multiplier for the dimensionality of the embedding vector.
        num_blocks          = 3,            # Number of residual blocks per resolution.
        attn_resolutions    = [32,16,8],    # List of resolutions with self-attention.
        dropout             = 0.10,         # List of resolutions with self-attention.
        label_dropout       = 0,            # Dropout probability of class labels for classifier-free guidance.
        training_mode='',
        linear_probing=False,
    ):
        super().__init__()
        self.label_dropout = label_dropout
        self.training_mode = training_mode
        emb_channels = model_channels * channel_mult_emb
        init = dict(init_mode='kaiming_uniform', init_weight=np.sqrt(1/3), init_bias=np.sqrt(1/3))
        init_zero = dict(init_mode='kaiming_uniform', init_weight=0, init_bias=0)
        block_kwargs = dict(emb_channels=emb_channels, channels_per_head=64, dropout=dropout, init=init, init_zero=init_zero)

        # Mapping.
        self.map_noise = PositionalEmbedding(num_channels=model_channels)
        self.map_augment = Linear(in_features=augment_dim, out_features=model_channels, bias=False, **init_zero) if augment_dim else None
        self.map_layer0 = Linear(in_features=model_channels, out_features=emb_channels, **init)
        self.map_layer1 = Linear(in_features=emb_channels, out_features=emb_channels, **init)
        self.map_label = Linear(in_features=label_dim, out_features=emb_channels, bias=False, init_mode='kaiming_normal', init_weight=np.sqrt(label_dim)) if label_dim else None
        if self.training_mode.lower() == 'ctm':
            self.map_layer0_s = Linear(in_features=model_channels, out_features=emb_channels, **init)
            self.map_layer1_s = Linear(in_features=emb_channels, out_features=emb_channels, **init)

        # Encoder.
        self.enc = torch.nn.ModuleDict()
        cout = in_channels
        for level, mult in enumerate(channel_mult):
            res = img_resolution >> level
            if level == 0:
                cin = cout
                cout = model_channels * mult
                self.enc[f'{res}x{res}_conv'] = Conv2d(in_channels=cin, out_channels=cout, kernel=3, **init)
            else:
                self.enc[f'{res}x{res}_down'] = UNetBlock(in_channels=cout, out_channels=cout, down=True,
                                                          training_mode=training_mode, **block_kwargs)
            for idx in range(num_blocks):
                cin = cout
                cout = model_channels * mult
                self.enc[f'{res}x{res}_block{idx}'] = UNetBlock(in_channels=cin, out_channels=cout, attention=(res in attn_resolutions),
                                                                training_mode=training_mode, **block_kwargs)
        skips = [block.out_channels for block in self.enc.values()]

        # Decoder.
        self.dec = torch.nn.ModuleDict()
        for level, mult in reversed(list(enumerate(channel_mult))):
            res = img_resolution >> level
            if level == len(channel_mult) - 1:
                self.dec[f'{res}x{res}_in0'] = UNetBlock(in_channels=cout, out_channels=cout, attention=True,
                                                         training_mode=training_mode, **block_kwargs)
                self.dec[f'{res}x{res}_in1'] = UNetBlock(in_channels=cout, out_channels=cout,
                                                         training_mode=training_mode, **block_kwargs)
            else:
                self.dec[f'{res}x{res}_up'] = UNetBlock(in_channels=cout, out_channels=cout, up=True,
                                                        training_mode=training_mode, **block_kwargs)
            for idx in range(num_blocks + 1):
                cin = cout + skips.pop()
                cout = model_channels * mult
                self.dec[f'{res}x{res}_block{idx}'] = UNetBlock(in_channels=cin, out_channels=cout, attention=(res in attn_resolutions),
                                                                training_mode=training_mode, **block_kwargs)
        self.out_norm = GroupNorm(num_channels=cout)
        self.out_conv = Conv2d(in_channels=cout, out_channels=out_channels, kernel=3, **init_zero)

    def convert_to_fp16(self):
        """
        Convert the torso of the model to float16.
        """
        
        # self.map_label.apply(convert_module_to_f16)
        # self.map_augment.apply(convert_module_to_f16)
        # self.map_layer0.apply(convert_module_to_f16)
        # self.map_layer1.apply(convert_module_to_f16)
        
        # if self.training_mode.lower() == 'ctm':
        #     self.map_layer0_s.apply(convert_module_to_f16)
        #     self.map_layer1_s.apply(convert_module_to_f16)

        self.enc.apply(convert_module_to_f16)
        self.dec.apply(convert_module_to_f16)
        
        self.out_norm.apply(convert_module_to_f16)
        self.out_conv.apply(convert_module_to_f16)
        
        # self.input_blocks.apply(convert_module_to_f16)
        # self.middle_block.apply(convert_module_to_f16)
        # self.output_blocks.apply(convert_module_to_f16)
    
    def convert_to_bf16(self):
        """
        Convert the torso of the model to bfloat16.
        """
        # self.map_label.apply(convert_module_to_bf16)
        # self.map_augment.apply(convert_module_to_bf16)
        # self.map_layer0.apply(convert_module_to_bf16)
        # self.map_layer1.apply(convert_module_to_bf16)
        
        # if self.training_mode.lower() == 'ctm':
        #     self.map_layer0_s.apply(convert_module_to_bf16)
        #     self.map_layer1_s.apply(convert_module_to_bf16)

        self.enc.apply(convert_module_to_bf16)
        self.dec.apply(convert_module_to_bf16)
        
        self.out_norm.apply(convert_module_to_bf16)
        self.out_conv.apply(convert_module_to_bf16)
        
        # self.input_blocks.apply(convert_module_to_bf16)
        # self.middle_block.apply(convert_module_to_bf16)
        # self.output_blocks.apply(convert_module_to_bf16)

    def convert_to_fp32(self):
        """
        Convert the torso of the model to float32.
        """
        # self.map_label.apply(convert_module_to_f32)
        # self.map_augment.apply(convert_module_to_f32)
        # self.map_layer0.apply(convert_module_to_f32)
        # self.map_layer1.apply(convert_module_to_f32)
        
        # if self.training_mode.lower() == 'ctm':
        #     self.map_layer0_s.apply(convert_module_to_f32)
        #     self.map_layer1_s.apply(convert_module_to_f32)

        self.enc.apply(convert_module_to_f32)
        self.dec.apply(convert_module_to_f32)
        
        self.out_norm.apply(convert_module_to_f32)
        self.out_conv.apply(convert_module_to_f32)
        
        # self.input_blocks.apply(convert_module_to_f32)
        # self.middle_block.apply(convert_module_to_f32)
        # self.output_blocks.apply(convert_module_to_f32)
        
    def forward(self, x, noise_labels, noise_labels_s, class_labels, augment_labels=None):
        # Mapping.
        emb = self.map_noise(noise_labels)
        if self.map_augment is not None and augment_labels is not None:
            emb = emb + self.map_augment(augment_labels)
        emb = silu(self.map_layer0(emb))
        emb = self.map_layer1(emb)
        if self.map_label is not None:
            tmp = class_labels
            if self.training and self.label_dropout:
                tmp = tmp * (torch.rand([x.shape[0], 1], device=x.device) >= self.label_dropout).to(tmp.dtype)
            emb = emb + self.map_label(tmp)
        emb = silu(emb)

        if noise_labels_s != None:
            emb_s = self.map_noise(noise_labels_s)
            if self.map_augment is not None and augment_labels is not None:
                emb_s = emb_s + self.map_augment(augment_labels)
            emb_s = silu(self.map_layer0_s(emb_s))
            emb_s = self.map_layer1_s(emb_s)
            if self.map_label is not None:
                tmp = class_labels
                if self.training and self.label_dropout:
                    tmp = tmp * (torch.rand([x.shape[0], 1], device=x.device) >= self.label_dropout).to(tmp.dtype)
                emb_s = emb_s + self.map_label(tmp)
            emb_s = silu(emb_s)

        # Encoder.
        skips = []
        for block in self.enc.values():
            x = block(x, emb, emb_s=None if noise_labels_s == None else emb_s) if isinstance(block, UNetBlock) else block(x)
            skips.append(x)

        # Decoder.
        for block in self.dec.values():
            if x.shape[1] != block.in_channels:
                x = torch.cat([x, skips.pop()], dim=1)
            x = block(x, emb, emb_s=None if noise_labels_s == None else emb_s)
        x = self.out_conv(silu(self.out_norm(x)))
        return x

#----------------------------------------------------------------------------
# Preconditioning corresponding to the variance preserving (VP) formulation
# from the paper "Score-Based Generative Modeling through Stochastic
# Differential Equations".

@persistence.persistent_class
class VPPrecond(torch.nn.Module):
    def __init__(self,
        img_resolution,                 # Image resolution.
        img_channels,                   # Number of color channels.
        label_dim       = 0,            # Number of class labels, 0 = unconditional.
        use_fp16        = False,        # Execute the underlying model at FP16 precision?
        use_bf16        = False,        # Execute the underlying model at BF16 precision?
        beta_d          = 19.9,         # Extent of the noise level schedule.
        beta_min        = 0.1,          # Initial slope of the noise level schedule.
        M               = 1000,         # Original number of timesteps in the DDPM formulation.
        epsilon_t       = 1e-5,         # Minimum t-value used during training.
        model_type      = 'SongUNet',   # Class name of the underlying model.
        **model_kwargs,                 # Keyword arguments for the underlying model.
    ):
        super().__init__()
        self.img_resolution = img_resolution
        self.img_channels = img_channels
        self.label_dim = label_dim
        self.use_fp16 = use_fp16
        self.use_bf16 = use_bf16
        self.beta_d = beta_d
        self.beta_min = beta_min
        self.M = M
        self.epsilon_t = epsilon_t
        self.sigma_min = float(self.sigma(epsilon_t))
        self.sigma_max = float(self.sigma(1))
        self.model = globals()[model_type](img_resolution=img_resolution, in_channels=img_channels, out_channels=img_channels, label_dim=label_dim, **model_kwargs)

    def forward(self, x, sigma, class_labels=None, force_fp32=False, **model_kwargs):
        x = x.to(torch.float32)
        sigma = sigma.to(torch.float32).reshape(-1, 1, 1, 1)
        class_labels = None if self.label_dim == 0 else torch.zeros([1, self.label_dim], device=x.device) if class_labels is None else class_labels.to(torch.float32).reshape(-1, self.label_dim)
        # dtype = torch.float16 if (self.use_fp16 and not force_fp32 and x.device.type == 'cuda') else torch.float32

        if self.use_fp16 and not force_fp32 and x.device.type == 'cuda':
            dtype = torch.float16
        elif self.use_bf16 and not force_fp32 and x.device.type == 'cuda':
            dtype = torch.bfloat16
        else:
            dtype = torch.float32
            
        c_skip = 1
        c_out = -sigma
        c_in = 1 / (sigma ** 2 + 1).sqrt()
        c_noise = (self.M - 1) * self.sigma_inv(sigma)

        F_x = self.model((c_in * x).to(dtype), c_noise.flatten(), class_labels=class_labels, **model_kwargs)
        assert F_x.dtype == dtype
        D_x = c_skip * x + c_out * F_x.to(torch.float32)
        return D_x

    def sigma(self, t):
        t = torch.as_tensor(t)
        return ((0.5 * self.beta_d * (t ** 2) + self.beta_min * t).exp() - 1).sqrt()

    def sigma_inv(self, sigma):
        sigma = torch.as_tensor(sigma)
        return ((self.beta_min ** 2 + 2 * self.beta_d * (1 + sigma ** 2).log()).sqrt() - self.beta_min) / self.beta_d

    def round_sigma(self, sigma):
        return torch.as_tensor(sigma)
    
    def convert_to_fp16(self):
        """
        Convert the torso of the model to float16.
        """

        self.model.convert_to_fp16()
    
    def convert_to_bf16(self):
        """
        Convert the torso of the model to bfloat16.
        """
        self.model.convert_to_bf16()

    def convert_to_fp32(self):
        """
        Convert the torso of the model to float32.
        """
        self.model.convert_to_fp32()

#----------------------------------------------------------------------------
# Preconditioning corresponding to the variance exploding (VE) formulation
# from the paper "Score-Based Generative Modeling through Stochastic
# Differential Equations".

@persistence.persistent_class
class VEPrecond(torch.nn.Module):
    def __init__(self,
        img_resolution,                 # Image resolution.
        img_channels,                   # Number of color channels.
        label_dim       = 0,            # Number of class labels, 0 = unconditional.
        use_fp16        = False,        # Execute the underlying model at FP16 precision?
        use_bf16        = False,        # Execute the underlying model at BF16 precision?
        sigma_min       = 0.02,         # Minimum supported noise level.
        sigma_max       = 100,          # Maximum supported noise level.
        model_type      = 'SongUNet',   # Class name of the underlying model.
        **model_kwargs,                 # Keyword arguments for the underlying model.
    ):
        super().__init__()
        self.img_resolution = img_resolution
        self.img_channels = img_channels
        self.label_dim = label_dim
        self.use_fp16 = use_fp16
        self.use_bf16 = use_bf16
        self.sigma_min = sigma_min
        self.sigma_max = sigma_max
        self.model = globals()[model_type](img_resolution=img_resolution, in_channels=img_channels, out_channels=img_channels, label_dim=label_dim, **model_kwargs)

    def forward(self, x, sigma, class_labels=None, force_fp32=False, **model_kwargs):
        x = x.to(torch.float32)
        sigma = sigma.to(torch.float32).reshape(-1, 1, 1, 1)
        class_labels = None if self.label_dim == 0 else torch.zeros([1, self.label_dim], device=x.device) if class_labels is None else class_labels.to(torch.float32).reshape(-1, self.label_dim)
        
        # dtype = torch.float16 if (self.use_fp16 and not force_fp32 and x.device.type == 'cuda') else torch.float32
        if self.use_fp16 and not force_fp32 and x.device.type == 'cuda':
            dtype = torch.float16
        elif self.use_bf16 and not force_fp32 and x.device.type == 'cuda':
            dtype = torch.bfloat16
        else:
            dtype = torch.float32
            
        c_skip = 1
        c_out = sigma
        c_in = 1
        c_noise = (0.5 * sigma).log()

        F_x = self.model((c_in * x).to(dtype), c_noise.flatten(), class_labels=class_labels, **model_kwargs)
        assert F_x.dtype == dtype
        D_x = c_skip * x + c_out * F_x.to(torch.float32)
        return D_x

    def round_sigma(self, sigma):
        return torch.as_tensor(sigma)
    
    def convert_to_fp16(self):
        """
        Convert the torso of the model to float16.
        """

        self.model.convert_to_fp16()
    
    def convert_to_bf16(self):
        """
        Convert the torso of the model to bfloat16.
        """
        self.model.convert_to_bf16()

    def convert_to_fp32(self):
        """
        Convert the torso of the model to float32.
        """
        self.model.convert_to_fp32()
        
#----------------------------------------------------------------------------
# Preconditioning corresponding to improved DDPM (iDDPM) formulation from
# the paper "Improved Denoising Diffusion Probabilistic Models".

@persistence.persistent_class
class iDDPMPrecond(torch.nn.Module):
    def __init__(self,
        img_resolution,                     # Image resolution.
        img_channels,                       # Number of color channels.
        label_dim       = 0,                # Number of class labels, 0 = unconditional.
        use_fp16        = False,            # Execute the underlying model at FP16 precision?
        use_bf16        = False,        # Execute the underlying model at BF16 precision?
        C_1             = 0.001,            # Timestep adjustment at low noise levels.
        C_2             = 0.008,            # Timestep adjustment at high noise levels.
        M               = 1000,             # Original number of timesteps in the DDPM formulation.
        model_type      = 'DhariwalUNet',   # Class name of the underlying model.
        **model_kwargs,                     # Keyword arguments for the underlying model.
    ):
        super().__init__()
        self.img_resolution = img_resolution
        self.img_channels = img_channels
        self.label_dim = label_dim
        self.use_fp16 = use_fp16
        self.use_bf16 = use_bf16
        self.C_1 = C_1
        self.C_2 = C_2
        self.M = M
        self.model = globals()[model_type](img_resolution=img_resolution, in_channels=img_channels, out_channels=img_channels*2, label_dim=label_dim, **model_kwargs)

        u = torch.zeros(M + 1)
        for j in range(M, 0, -1): # M, ..., 1
            u[j - 1] = ((u[j] ** 2 + 1) / (self.alpha_bar(j - 1) / self.alpha_bar(j)).clip(min=C_1) - 1).sqrt()
        self.register_buffer('u', u)
        self.sigma_min = float(u[M - 1])
        self.sigma_max = float(u[0])

    def forward(self, x, sigma, class_labels=None, force_fp32=False, **model_kwargs):
        x = x.to(torch.float32)
        sigma = sigma.to(torch.float32).reshape(-1, 1, 1, 1)
        class_labels = None if self.label_dim == 0 else torch.zeros([1, self.label_dim], device=x.device) if class_labels is None else class_labels.to(torch.float32).reshape(-1, self.label_dim)
        # dtype = torch.float16 if (self.use_fp16 and not force_fp32 and x.device.type == 'cuda') else torch.float32
        if self.use_fp16 and not force_fp32 and x.device.type == 'cuda':
            dtype = torch.float16
        elif self.use_bf16 and not force_fp32 and x.device.type == 'cuda':
            dtype = torch.bfloat16
        else:
            dtype = torch.float32
            
        c_skip = 1
        c_out = -sigma
        c_in = 1 / (sigma ** 2 + 1).sqrt()
        c_noise = self.M - 1 - self.round_sigma(sigma, return_index=True).to(torch.float32)

        F_x = self.model((c_in * x).to(dtype), c_noise.flatten(), class_labels=class_labels, **model_kwargs)
        assert F_x.dtype == dtype
        D_x = c_skip * x + c_out * F_x[:, :self.img_channels].to(torch.float32)
        return D_x

    def alpha_bar(self, j):
        j = torch.as_tensor(j)
        return (0.5 * np.pi * j / self.M / (self.C_2 + 1)).sin() ** 2

    def round_sigma(self, sigma, return_index=False):
        sigma = torch.as_tensor(sigma)
        index = torch.cdist(sigma.to(self.u.device).to(torch.float32).reshape(1, -1, 1), self.u.reshape(1, -1, 1)).argmin(2)
        result = index if return_index else self.u[index.flatten()].to(sigma.dtype)
        return result.reshape(sigma.shape).to(sigma.device)
    
    def convert_to_fp16(self):
        """
        Convert the torso of the model to float16.
        """

        self.model.convert_to_fp16()
    
    def convert_to_bf16(self):
        """
        Convert the torso of the model to bfloat16.
        """
        self.model.convert_to_bf16()

    def convert_to_fp32(self):
        """
        Convert the torso of the model to float32.
        """
        self.model.convert_to_fp32()
        
#----------------------------------------------------------------------------
# Improved preconditioning proposed in the paper "Elucidating the Design
# Space of Diffusion-Based Generative Models" (EDM).

@persistence.persistent_class
class EDMPrecond(torch.nn.Module):
    def __init__(self,
        img_resolution,                     # Image resolution.
        img_channels,                       # Number of color channels.
        label_dim       = 0,                # Number of class labels, 0 = unconditional.
        use_fp16        = False,            # Execute the underlying model at FP16 precision?
        use_bf16        = False,            # Execute the underlying model at BF16 precision?
        sigma_min       = 0,                # Minimum supported noise level.
        sigma_max       = float('inf'),     # Maximum supported noise level.
        sigma_data      = 0.5,              # Expected standard deviation of the training data.
        model_type      = 'DhariwalUNet',   # Class name of the underlying model.
        **model_kwargs,                     # Keyword arguments for the underlying model.
    ):
        super().__init__()
        self.img_resolution = img_resolution
        self.img_channels = img_channels
        self.label_dim = label_dim
        self.use_fp16 = use_fp16
        self.use_bf16 = use_bf16
        self.sigma_min = sigma_min
        self.sigma_max = sigma_max
        self.sigma_data = sigma_data
        self.model = globals()[model_type](img_resolution=img_resolution, in_channels=img_channels,
                                           out_channels=img_channels, label_dim=label_dim, **model_kwargs)

    def forward(self, x, sigma, class_labels=None, force_fp32=False, **model_kwargs):
        x = x.to(torch.float32)
        sigma = sigma.to(torch.float32).reshape(-1, 1, 1, 1)
        class_labels = None if self.label_dim == 0 else torch.zeros([1, self.label_dim], device=x.device) if class_labels is None else class_labels.to(torch.float32).reshape(-1, self.label_dim)
        # dtype = torch.float16 if (self.use_fp16 and not force_fp32 and x.device.type == 'cuda') else torch.float32
        if self.use_fp16 and not force_fp32 and x.device.type == 'cuda':
            dtype = torch.float16
        elif self.use_bf16 and not force_fp32 and x.device.type == 'cuda':
            dtype = torch.bfloat16
        else:
            dtype = torch.float32

        c_skip = self.sigma_data ** 2 / (sigma ** 2 + self.sigma_data ** 2)
        c_out = sigma * self.sigma_data / (sigma ** 2 + self.sigma_data ** 2).sqrt()
        c_in = 1 / (self.sigma_data ** 2 + sigma ** 2).sqrt()
        c_noise = sigma.log() / 4

        F_x = self.model((c_in * x).to(dtype), c_noise.flatten(), class_labels=class_labels, **model_kwargs)
        assert F_x.dtype == dtype
        D_x = c_skip * x + c_out * F_x.to(torch.float32)
        return D_x

    def round_sigma(self, sigma):
        return torch.as_tensor(sigma)
    
    def convert_to_fp16(self):
        """
        Convert the torso of the model to float16.
        """

        self.model.convert_to_fp16()
    
    def convert_to_bf16(self):
        """
        Convert the torso of the model to bfloat16.
        """
        self.model.convert_to_bf16()

    def convert_to_fp32(self):
        """
        Convert the torso of the model to float32.
        """
        self.model.convert_to_fp32()

#----------------------------------------------------------------------------

#----------------------------------------------------------------------------
# Improved preconditioning proposed in the paper "Elucidating the Design
# Space of Diffusion-Based Generative Models" (EDM).

@persistence.persistent_class
class EDMPrecond_CTM(torch.nn.Module):
    def __init__(self,
        img_resolution,                     # Image resolution.
        img_channels,                       # Number of color channels.
        label_dim       = 0,                # Number of class labels, 0 = unconditional.
        use_fp16        = False,            # Execute the underlying model at FP16 precision?
        use_bf16        = False,            # Execute the underlying model at BF16 precision?
        sigma_min       = 0,                # Minimum supported noise level.
        sigma_max       = float('inf'),     # Maximum supported noise level.
        sigma_data      = 0.5,              # Expected standard deviation of the training data.
        model_type      = 'SongUNet',       # Class name of the underlying model.
        teacher = False,
        teacher_model_path = '',
        training_mode = '',
        arch='ncsn',
        linear_probing=False,
        condition_mode=None,
        sigma_data_end=None,
        cov_xy=None,
        inner_parametrization = 'edm',
        **model_kwargs,                     # Keyword arguments for the underlying model.
    ):
        super().__init__()
        self.teacher = teacher
        self.img_resolution = img_resolution
        self.img_channels = img_channels
        self.label_dim = label_dim
        self.use_fp16 = use_fp16
        self.use_bf16 = use_bf16
        self.sigma_min = sigma_min
        self.sigma_max = sigma_max
        self.sigma_data = sigma_data
        self.sigma_data_end = sigma_data_end
        self.cov_xy = cov_xy
        self.inner_parametrization = inner_parametrization
        
        self.eye = torch.eye(self.label_dim, device=dist_util.dev())
        if teacher:
            import pickle
            print(f'Loading network from "{teacher_model_path}"...')
            with open(teacher_model_path, 'rb') as f:
                self.model = pickle.load(f)['ema']
        else:
            if arch in ['ddpmpp', 'ncsnpp']:
                resample_filter = [1,1] if arch == 'ddpmpp' else [1,3,3,1]
                channel_mult_noise = 1 if arch == 'ddpmpp' else 2
                encoder_type = 'standard' if arch == 'ddpmpp' else 'residual'
                embedding_type = 'positional' if arch == 'ddpmpp' else 'fourier'
                self.model = globals()[model_type](img_resolution=img_resolution, in_channels=img_channels,
                                                   out_channels=img_channels, label_dim=label_dim,
                                                   training_mode=training_mode, resample_filter=resample_filter,
                                                   channel_mult_noise=channel_mult_noise, encoder_type=encoder_type,
                                                   embedding_type=embedding_type, linear_probing=linear_probing,
                                                   condition_mode=condition_mode,
                                                   **model_kwargs)
            else:
                self.model = globals()[model_type](img_resolution=img_resolution, in_channels=img_channels,
                                                   out_channels=img_channels, label_dim=label_dim,
                                                   training_mode=training_mode, linear_probing=linear_probing,
                                                   condition_mode=condition_mode,
                                                   **model_kwargs)

    def get_c_in(self, sigma):
        if self.inner_parametrization == 'edm':
            c_in = 1 / (sigma**2 + self.sigma_data**2) ** 0.5
        elif self.inner_parametrization == 'cm_ddbm':
            c = 1
            sigma = sigma - self.sigma_min
            snrT_div_snrt: torch.Tensor = (sigma**2) / (self.sigma_max**2)
            a_t = snrT_div_snrt.detach().clone()
            b_t = 1. - snrT_div_snrt
            
            A = a_t.square() * (self.sigma_data_end**2) + b_t.square() * (self.sigma_data**2) + (2*a_t*b_t*self.cov_xy) + ((c**2) * (sigma**2) * b_t)
            c_in = 1 / A.sqrt()
        elif self.inner_parametrization == 'ddbm':
            c = 1
            snrT_div_snrt: torch.Tensor = (sigma**2) / (self.sigma_max**2)
            a_t = snrT_div_snrt.detach().clone()
            b_t = 1. - snrT_div_snrt
            
            A = a_t.square() * (self.sigma_data_end**2) + b_t.square() * (self.sigma_data**2) + (2*a_t*b_t*self.cov_xy) + ((c**2) * (sigma**2) * b_t)
            c_in = 1 / A.sqrt()
        else:
            raise NotImplementedError(f"c_in for '{self.inner_parametrization}' not implemented yet in network.py.")
        return c_in
    
    def unrescaling_t(self, rescaled_t):
        return torch.exp(rescaled_t / 250.) - 1e-44

    # NOTE 8th July: 'x_T' is also rescaled below! 
    #                 But, I have just kept the name as 'x_T' due to originally having started off with such a naming scheme.
    def forward(self, rescaled_x, rescaled_t, s=None, teacher=False, x_T=None, **model_kwargs):
        # print('model_kwargs', model_kwargs.keys())
        # exit()
        class_labels = None if self.label_dim == 0 else torch.zeros([1, self.label_dim], device=rescaled_x.device) \
            if model_kwargs == {} else self.eye[model_kwargs['y']].reshape(-1, self.label_dim)
        
        # dtype = torch.float16 if self.use_fp16 and rescaled_x.device.type == 'cuda' else torch.float32
        # if self.use_fp16 and rescaled_x.device.type == 'cuda':
        if self.use_fp16:
            dtype = torch.float16
        # elif self.use_bf16 and rescaled_x.device.type == 'cuda':
        elif self.use_bf16:
            dtype = torch.bfloat16
        else:
            dtype = torch.float32

        if self.teacher:
            raise NotImplementedError()
            #with torch.no_grad():
            sigma = self.unrescaling_t(rescaled_t)
            c_in = append_dims(self.get_c_in(sigma), rescaled_x.ndim)
            x = rescaled_x / c_in
            D_x = self.model(x.to(dtype), sigma.flatten(), class_labels=class_labels)
            c_skip = append_dims(self.sigma_data ** 2 / (sigma ** 2 + self.sigma_data ** 2), rescaled_x.ndim)
            c_out = append_dims(sigma * self.sigma_data / (sigma ** 2 + self.sigma_data ** 2).sqrt(), rescaled_x.ndim)
            F_x = (D_x - c_skip * x) / c_out
        else:
            # # print('org t', rescaled_t)
            t = self.unrescaling_t(rescaled_t)
            # # print('new t', t.min(), t.max())
            t = (t + 1e-44).log() / 4
            assert not t.isinf().any()
            assert not t.isnan().any()
            # # print('new t', t)
            # # exit()
            if s != None:
                s = self.unrescaling_t(s)
                # print('old s', s.min(), s.max())
                s = (s + 1e-44).log() / 4
                # print('new s', s.min(), s.max())
                # exit()
                # assert not s.isinf().any()
                # assert not s.isnan().any()
            # t = rescaled_t
            F_x = self.model(x=rescaled_x.to(dtype), 
                            noise_labels=t.flatten(), 
                            noise_labels_s=None if s == None else s.flatten(), 
                            class_labels=class_labels,
                            x_T=x_T.to(dtype).clone() if x_T is not None else None)
        #assert F_x.dtype == dtype
        return F_x

    def round_sigma(self, sigma):
        return torch.as_tensor(sigma)

    # def convert_to_fp16(self):
    #     pass

    # def convert_to_fp32(self):
    #     pass
    
    # def convert_to_bf16(self):
    #     pass
    
    def convert_to_fp16(self):
        """
        Convert the torso of the model to float16.
        """
        if self.teacher:
            # print('this is teacher!!!')
            pass
        else:
            self.model.convert_to_fp16()
    
    def convert_to_bf16(self):
        """
        Convert the torso of the model to bfloat16.
        """
        if self.teacher:
            # print('this is teacher!!!')
            pass
        else:
            self.model.convert_to_bf16()

    def convert_to_fp32(self):
        """
        Convert the torso of the model to float32.
        """
        self.model.convert_to_fp32()
#----------------------------------------------------------------------------
