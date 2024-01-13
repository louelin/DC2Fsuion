import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.checkpoint as checkpoint
from timm.models.layers import DropPath, trunc_normal_, to_2tuple
# from .configs import get_config
# from .configs_Cos import get_CycleMorph_config
# from mmaction.utils import get_root_logger
import einops
from functools import reduce
from operator import mul
from .STN import SpatialTransformer, Re_SpatialTransformer
from einops import rearrange
from .our_utils import fusion_strategy

class Mlp(nn.Module):
    """ Multilayer perceptron."""

    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


def window_partition(x, window_size):
    """
    Args:
        x: (B, D, H, W, C)
        window_size (tuple[int]): window size

    Returns:
        windows: (B*num_windows, window_size*window_size*window_size, C)
    """
    B, D, H, W, C = x.shape
    x = x.view(B, D // window_size[0], window_size[0], H // window_size[1], window_size[1], W // window_size[2],
               window_size[2], C)  # (1,20,2,24,2,28,2,48)
    windows = x.permute(0, 1, 3, 5, 2, 4, 6, 7).contiguous().view(-1, reduce(mul, window_size), C)
    return windows  # (13440,8,48)


def window_reverse(windows, window_size, B, D, H, W):
    """
    Args:
        windows: (B*num_windows, window_size, window_size, window_size, C)
        window_size (tuple[int]): Window size
        H (int): Height of image
        W (int): Width of image
        D (int): Depth of image

    Returns:
        x: (B, D, H, W, C)
    """
    x = windows.view(B, D // window_size[0], H // window_size[1], W // window_size[2], window_size[0], window_size[1],
                     window_size[2], -1)
    x = x.permute(0, 1, 4, 2, 5, 3, 6, 7).contiguous().view(B, D, H, W, -1)
    return x


def get_window_size(x_size, window_size, shift_size=None):
    use_window_size = list(window_size)
    if shift_size is not None:
        use_shift_size = list(shift_size)
    for i in range(len(x_size)):
        if x_size[i] <= window_size[i]:
            use_window_size[i] = x_size[i]
            if shift_size is not None:
                use_shift_size[i] = 0

    return tuple(use_window_size)

class CrossWindowAttention3D(nn.Module):
    """ Window based multi-head self attention (W-MSA) module with relative position bias.
    It supports both of shifted and non-shifted window.
    Args:
        dim (int): Number of input channels.
        window_size (tuple[int]): The temporal length, height and width of the window.
        num_heads (int): Number of attention heads.
        qkv_bias (bool, optional):  If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set
        attn_drop (float, optional): Dropout ratio of attention weight. Default: 0.0
        proj_drop (float, optional): Dropout ratio of output. Default: 0.0
    """

    def __init__(self, dim, window_size, num_heads, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.dim = dim
        self.window_size = window_size  # Wd, Wh, Ww
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        # self.q = nn.Linear(dim, dim, bias=qkv_bias)
        # self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.q = nn.Linear(dim, dim, bias=qkv_bias)         
        self.k = nn.Linear(dim, dim, bias=qkv_bias)
        self.v = nn.Linear(dim, dim, bias=qkv_bias)  

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, xa):
        """ Forward function.
        Args:
            x: input features with shape of (num_windows*B, N, C)
            xa: input features with shape of (num_windows*B, M, C) C是channel
        """
        B_, N, C = x.shape  # (13440,8,48)
        _, M, _ = xa.shape  # (13440,8,48)

        # print('x shape:', x.shape)
        # print("dim : " , self.dim)
        q = self.q(x)
        k = self.k(xa)
        v = self.v(x)

        q = q.reshape(B_, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        # print("k shape is : " , k.shape)
        # print('q shape:', q.shape)
        # for name, param in self.q.named_parameters():
        #     if 'weight' in name:
        #         print(name, param.shape)
        k = k.reshape(B_, M, self.num_heads, C // self.num_heads).permute(0,2,1,3)
        v = v.reshape(B_, M, self.num_heads, C // self.num_heads).permute(0,2,1,3)
        # qv = self.qv(x).reshape(B_, M, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        # q, v = qv[0], qv[1]  # (13440,3,216,16)
        
        # q = self.q(x).reshape(B_, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3) #(13440,3,8,16)

        q = q * self.scale
        attn = q @ k.transpose(-2, -1)

        attn = self.softmax(attn)

        attn = self.attn_drop(attn)  # (13440,3,8,8)

        x = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)  # (13440,8,48)
        return x, q, k, v


class CrossTransformerBlock3D(nn.Module):
    """ Swin Transformer Block.

    Args:
        dim (int): Number of input channels.
        num_heads (int): Number of attention heads.
        window_size (tuple[int]): Window size.
        shift_size (tuple[int]): Shift size for SW-MSA.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
        qkv_bias (bool, optional): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set.
        drop (float, optional): Dropout rate. Default: 0.0
        attn_drop (float, optional): Attention dropout rate. Default: 0.0
        drop_path (float, optional): Stochastic depth rate. Default: 0.0
        act_layer (nn.Module, optional): Activation layer. Default: nn.GELU
        norm_layer (nn.Module, optional): Normalization layer.  Default: nn.LayerNorm
    """

    def __init__(self, dim, num_heads, window_size=(4, 4, 4), hidden_channels=16, kk=3, offset_range_factor=2,
                 mlp_ratio=4., qkv_bias=True, qk_scale=None, drop=0., attn_drop=0., drop_path=0.,dwconv_kernel_size=7,
                 act_layer=nn.GELU, norm_layer=nn.LayerNorm, use_checkpoint=False):
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.mlp_ratio = mlp_ratio
        self.use_checkpoint = use_checkpoint
        self.hidden_channels = hidden_channels
        self.kk = kk  ## !!!kk的shape
        self.offset_range_factor = offset_range_factor  # !!!!
        self.dwconv_kernel_size = dwconv_kernel_size

        self.norm1 = norm_layer(dim)
        self.cross_attn = CrossWindowAttention3D(
            dim, window_size=self.window_size, num_heads=num_heads,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)

        self.conv_offset = nn.Sequential(
            nn.Conv3d(dim * 2, dim * 2, self.kk, 1, self.kk // 2 , groups= dim * 2),
            LayerNormProxy( dim * 2),
            nn.GELU(),
            nn.Conv3d( dim * 2, 3, 1, 1, 0, bias=False)
        )  # b*g c d h w
        self.proj_q = nn.Conv3d(dim*2 , dim*2 ,1 , 1 ,0)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.projection = nn.Conv3d(dim, dim, kernel_size=1)

        self.dwconv3x3 = nn.Sequential(
            nn.Conv3d(
                dim*2, dim,
                kernel_size=self.dwconv_kernel_size,
                padding=self.dwconv_kernel_size // 2,
                groups=dim
            ),
            nn.LazyBatchNorm3d(dim),#选择layer还是batch？
            nn.GELU()
        )
        self.channel_interaction = nn.Sequential(
            nn.AdaptiveAvgPool3d(1),
            nn.Conv3d(dim, dim // 8, kernel_size=1),
            LayerNormProxy(dim // 8),
            nn.GELU(),
            nn.Conv3d(dim // 8, dim, kernel_size=1),  # 在Attention分支通道数减半
        )
        self.spatial_interaction = nn.Sequential(
            nn.Conv3d(dim, dim // 8, kernel_size=1),
            LayerNormProxy(dim // 8),
            nn.GELU(),
            nn.Conv3d(dim //8, 1, kernel_size=1)  # 最终空间信息输出通道为1
        )


        self.proj = nn.Linear(dim*2, dim)
        self.proj_drop = nn.Dropout(drop)


    @torch.no_grad()
    def _get_ref_points(self, D_key, H_key, W_key, B, dtype, device):

        ref_z, ref_y, ref_x = torch.meshgrid(
            torch.linspace(0.5, D_key - 0.5, D_key, dtype=dtype, device=device),
            torch.linspace(0.5, H_key - 0.5, H_key, dtype=dtype, device=device),
            torch.linspace(0.5, W_key - 0.5, W_key, dtype=dtype, device=device))
        ref = torch.stack((ref_z, ref_y, ref_x), -1)
        ref[..., 2].div_(D_key).mul_(2).sub_(1)
        ref[..., 1].div_(W_key).mul_(2).sub_(1)
        ref[..., 0].div_(H_key).mul_(2).sub_(1)
        ref = ref[None, ...].expand(B, -1, -1, -1, -1)  # B * g H W 2
        return ref

    def forward_part1(self, x, xa):
        B, D, H, W, C_ = x.shape
        window_size = get_window_size((D, H, W), self.window_size)  # (2,2,2)

        x = self.norm1(x)
        # pad feature maps to multiples of window size
        pad_l = pad_t = pad_d0 = 0
        pad_d1 = (window_size[0] - D % window_size[0]) % window_size[0]
        pad_b = (window_size[1] - H % window_size[1]) % window_size[1]
        pad_r = (window_size[2] - W % window_size[2]) % window_size[2]  # (0……0)
        x = F.pad(x, (0, 0, pad_l, pad_r, pad_t, pad_b, pad_d0, pad_d1))
        xa = F.pad(xa, (0, 0, pad_l, pad_r, pad_t, pad_b, pad_d0, pad_d1))
        _, Dp, Hp, Wp, _ = x.shape  # (1,40,48,56,48)
        dtype, device = x.dtype, x.device

        # concat & offset calculating
        concat_img = torch.cat([x, xa], dim=-1)
        concat_img = einops.rearrange(concat_img, 'n d h w c -> n c d h w')
        concat_img = self.proj_q(concat_img)
        offsets = self.conv_offset(concat_img)
        Dk, Hk, Wk = offsets.size(2), offsets.size(3), offsets.size(4)


        if self.offset_range_factor > 0:
            offset_range = torch.tensor([1.0 / Dk,1.0 / Hk, 1.0 / Wk], device=device).reshape(1, 3, 1,1, 1)
            offsets = offsets.tanh().mul(offset_range).mul(self.offset_range_factor)
            

        # resize offset
        offsets = einops.rearrange(offsets, 'b p d h w -> b d h w p')  # offsets: (1, 13, 16, 18, 3)
        # use the number of offset point and batch size to get reference point
        reference = self._get_ref_points(Dk, Hk, Wk, B, dtype, device)

        # offset + ref
        if self.offset_range_factor >= 0:
            pos = offsets + reference
        else:
            pos = (offsets + reference).tanh()


        xa_sampled = F.grid_sample(
            input=xa.permute(0, 4, 1, 2, 3),
            # input = xa ,
            grid=pos[..., (2, 1, 0)],  # y, x -> x, y
            mode='bilinear', align_corners=True)


        xa_sampled = xa_sampled.reshape(B, Dk, Hk, Wk, C_)  # B, D, H, W, C

        # partition windows
        x_windows = window_partition(x, window_size)  # B*nW, Wd*Wh*Ww, C  (13440,8,48) #

        B_, N, C = x_windows.shape
        x_area_windows = window_partition(xa_sampled, window_size)  # (-1)



        concat_window = torch.cat([x,xa_sampled],dim=4).permute(0,4,1,2,3)

        x_cnn = self.dwconv3x3(concat_window)
        channel_interaction = self.channel_interaction(x_cnn) #(1, 24, 1, 1, 1)
        x_cnn = self.projection(x_cnn) #（1, 24, 40, 48, 56）

# channel interaction
        attn_windows, q, k, v = self.cross_attn(x_windows, x_area_windows)  # B*nW, Wd*Wh*Ww, C 13440, 8 ,48
        x_cnn2v = torch.sigmoid(channel_interaction).reshape(-1, 1, self.num_heads, 1, C // self.num_heads) #(1, 1, 3, 1, 8)
        v = v.reshape(x_cnn2v.shape[0], -1, self.num_heads, N, C // self.num_heads) # v:(1, 26800, 3 , 8 , 8) q:(13440, 3, 8, 16)
        # *: -> [B, num_heads, num_heads, N, C/num_heads] --- C: C/2
        v = v * x_cnn2v #(1,26800, 3, 8 , 8)
        # reshape: -> [B*num_windows, num_heads, N, C/num_heads] --- C: C/2
        v = v.reshape(-1, self.num_heads, N, C // self.num_heads)  #(13440,3,8, 16)

        #transpose: -> [B * num_windows, num_heads, C / num_heads, N] - -- C: C / 2
        # @: multiply -> [B*num_windows, num_heads, N, N]
        attn = (q @ k.transpose(-2, -1))  # Q*K^{T} / sqrt(dk) (13440, 3, 8, 8)
        x_atten = (attn @ v).transpose(1, 2).reshape(B_, N, C) #(13440, 8 , 48)

# spatial interaction
#(B, D, H, W, C)-> (B, C, D, H, W)
        x_spatial = window_reverse(x_atten, window_size, B, Dp, Hp, Wp).permute(0, 4, 1, 2, 3) #(1, 48, 40, 48, 56)
        spatial_interaction = self.spatial_interaction(x_spatial) #期待输入维度是dim/2
        # sigmoid: -> [B, 1, D, H, W]
        # * -> [B, C, D, H, W] --- C: C/2
        x_cnn = (torch.sigmoid(spatial_interaction) * x_cnn).permute(0,2,3,4,1) # (1, 40, 48, 56, 48)
        # norm ！！！
        # [B, C, D, H, W] --> [num_windows*B, N, C] --- C: C/2
        x_cnn = window_partition(x_cnn, window_size) # 13440, 8 , 48

# concat
        x_atten = self.norm1(x_atten) #13440, 8 , 48
        # cat(): -> [num_windows*B, N, C] --- C: C
        x = torch.cat([x_cnn, x_atten], dim=2) #13440, 8 , 96
        # proj: -> [num_windows*B, N, C]
        x = self.proj(x)
        x = self.proj_drop(x)
        x_window = window_reverse(x, window_size, B, Dp, Hp, Wp)

        # merge windows
        # attn_windows = attn_windows.view(-1, *(window_size + (C,)))  # (13440,2,2,2,48)
        # x = window_reverse(attn_windows, window_size, B, Dp, Hp, Wp)  # B D' H' W' C #(1,40,48,56,48)

        if pad_d1 > 0 or pad_r > 0 or pad_b > 0:
            x_window = x_window[:, :D, :H, :W, :].contiguous()
        return x_window

    def forward_part2(self, x):
        return self.drop_path(self.mlp(self.norm1(x)))

    def forward(self, x, xa):
        """ Forward function.

        Args:
            x: Input feature, tensor size (B, D, H, W, C).

        """

        shortcut = x
        if self.use_checkpoint:
            x = checkpoint.checkpoint(self.forward_part1, x, xa)
        else:
            x = self.forward_part1(x, xa)
        x = shortcut + self.drop_path(x)

        if self.use_checkpoint:
            x = x + checkpoint.checkpoint(self.forward_part2, x)
        else:
            x = x + self.forward_part2(x)

        return x


class LayerNormProxy(nn.Module):

    def __init__(self, dim):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.dim = dim

    def forward(self, x):
        x = einops.rearrange(x, 'b c d h w -> b d h w c')
        x = self.norm(x)
        return einops.rearrange(x, 'b d h w c -> b c d h w')


# Statement for "Full Transformer": stride=kernel size, convolution degenerates into learnable pooling or upsample
class PatchMerging(nn.Module):
    """ Patch Merging Layer

    Args:
        dim (int): Number of input channels.
        norm_layer (nn.Module, optional): Normalization layer.  Default: nn.LayerNorm
    """

    def __init__(self, dim, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        # self.reduction = nn.Linear(8 * dim, 2 * dim, bias=False)
        self.down_conv = nn.Conv3d(dim, 2 * dim, (2, 2, 2), stride=2, padding=0)
        self.norm = norm_layer(2 * dim)

    def forward(self, x):
        """ Forward function.

        Args:
            x: Input feature, tensor size (B, D, H, W, C).
        """
        B, D, H, W, C = x.shape

        # padding
        pad_input = (H % 2 == 1) or (W % 2 == 1) or (D % 2 == 1)
        if pad_input:
            x = einops.rearrange(x, 'b d h w c -> b c d h w')
            x = F.pad(x, (0, W % 2, 0, H % 2, 0, D % 2))
            x = einops.rearrange(x, 'b c d h w -> b d h w c')
        x = einops.rearrange(x, 'b d h w c -> b c d h w')
        x = self.down_conv(x)
        x = einops.rearrange(x, 'b c d h w -> b d h w c')
        x = self.norm(x)

        return x


class PatchExpand(nn.Module):
    def __init__(self, dim, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.up_conv = nn.ConvTranspose3d(dim, dim // 2, (2, 2, 2), stride=2, padding=0)
        self.norm = norm_layer(dim // 2)

    def forward(self, x):
        """
        x: B,D,H,W,C
        """
        x = einops.rearrange(x, 'b d h w c -> b c d h w')
        x = self.up_conv(x)
        x = einops.rearrange(x, 'b c d h w -> b d h w c')
        x = self.norm(x)
        return x


class BasicLayer(nn.Module):
    """ A basic down-sample Transformer encoding layer for one stage.

    Args:
        dim (int): Number of feature channels
        depth (int): Depths of this stage.
        num_heads (int): Number of attention head.
        window_size (tuple[int]): Local window size. Default: (7,7,7).
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim. Default: 4.
        qkv_bias (bool, optional): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set.
        drop (float, optional): Dropout rate. Default: 0.0
        attn_drop (float, optional): Attention dropout rate. Default: 0.0
        drop_path (float | tuple[float], optional): Stochastic depth rate. Default: 0.0
        norm_layer (nn.Module, optional): Normalization layer. Default: nn.LayerNorm
        downsample (nn.Module | None, optional): Downsample layer at the end of the layer. Default: None
    """

    def __init__(self,
                 dim,  # dim=48\96\192\384
                 depth,
                 num_heads,
                 window_size,
                 mlp_ratio=4.,
                 qkv_bias=False,
                 qk_scale=None,
                 drop=0.,
                 attn_drop=0.,
                 drop_path=0.,
                 norm_layer=nn.LayerNorm,
                 downsample=None):
        super().__init__()
        self.window_size = window_size
        self.depth = depth

        # build blocks
        self.blocks = nn.ModuleList([
            CrossTransformerBlock3D(
                dim=dim,
                num_heads=num_heads,
                window_size=window_size,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop,
                attn_drop=attn_drop,
                drop_path=drop_path[i] if isinstance(drop_path, list) else drop_path,
                norm_layer=norm_layer,
            )
            for i in range(depth)])

        self.downsample = downsample
        if self.downsample is not None:
            self.downsample = downsample(dim=dim, norm_layer=norm_layer)

    def forward(self, x, xa):
        """ Forward function.

        Args:
            x: Input feature, tensor size (B, C, D, H, W).
            xa: Input feature a, tensor size (B, C, D, H, W).
        """
        # assert x.shape == xa.shape, "x xa must have same shape"
        # B, D, H, W, C = x.shape

        for blk in self.blocks:
            x = blk(x, xa)
            xa = blk(xa, x)

        if self.downsample is not None:
            x_down = self.downsample(x)
            xa_down = self.downsample(xa)
            return x, xa, x_down, xa_down
        return x, xa, x, xa


class BasicLayerUp(nn.Module):
    """ A basic up-sample Transformer encoder layer for one stage.

    Args:
        dim (int): Number of feature channels
        depth (int): Depths of this stage.
        num_heads (int): Number of attention head.
        window_size (tuple[int]): Local window size. Default: (7,7,7).
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim. Default: 4.
        qkv_bias (bool, optional): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set.
        drop (float, optional): Dropout rate. Default: 0.0
        attn_drop (float, optional): Attention dropout rate. Default: 0.0
        drop_path (float | tuple[float], optional): Stochastic depth rate. Default: 0.0
        norm_layer (nn.Module, optional): Normalization layer. Default: nn.LayerNorm
        upsample (nn.Module | None, optional): upsample layer at the end of the layer. Default: None
    """

    def __init__(self,
                 dim,
                 depth,
                 num_heads,
                 window_size,
                 mlp_ratio=4.,
                 qkv_bias=False,
                 qk_scale=None,
                 drop=0.,
                 attn_drop=0.,
                 drop_path=0.,
                 norm_layer=nn.LayerNorm,
                 upsample=None,
                 use_checkpoint=False):
        super().__init__()
        self.window_size = window_size
        self.depth = depth
        self.use_checkpoint = use_checkpoint

        # build blocks (self, dim, input_resolution, n_head, n_group, window_size, shift_size, mlp_ratio=2.,
        #                  drop=0., attn_drop=0.,qkv_bias=True,qk_scale=None, drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm,
        #                  use_pe=True, dwc_pe=False, no_off=False, fixed_pe=False):
        self.blocks = nn.ModuleList([
            CrossTransformerBlock3D(
                dim=dim,
                num_heads=num_heads,
                window_size=window_size,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop,
                attn_drop=attn_drop,
                drop_path=drop_path[i] if isinstance(drop_path, list) else drop_path,
                norm_layer=norm_layer
            )
            for i in range(depth)])

        self.upsample = upsample
        if self.upsample is not None:
            self.upsample = upsample(dim=dim, norm_layer=norm_layer)

    def forward(self, x, xa):
        """ Forward function.

        Args:
            x: Input feature, tensor size (B, C, D, H, W).
            xa: Input feature a, tensor size (B, C, D, H, W).
        """
        # assert x.shape == xa.shape, "x xa must have same shape"
        # B, D, H, W, C = x.shape

        for blk in self.blocks:
            x = blk(x, xa)
            xa = blk(xa, x)
            # print(x.shape, xa.shape)

        if self.upsample is not None:
            x_up = self.upsample(x)
            xa_up = self.upsample(xa)
            return x, xa, x_up, xa_up
        return x, xa, x, xa


class PatchEmbed3D(nn.Module):
    """ Video to Patch Embedding.

    Args:
        patch_size (int): Patch token size. Default: (2,4,4).
        in_chans (int): Number of input video channels. Default: 3.
        embed_dim (int): Number of linear projection output channels. Default: 96.
        norm_layer (nn.Module, optional): Normalization layer. Default: None
    """

    def __init__(self, patch_size=(4, 4, 4), in_chans=3, embed_dim=96, norm_layer=None):
        super().__init__()
        self.patch_size = patch_size

        self.in_chans = in_chans
        self.embed_dim = embed_dim

        self.proj = nn.Conv3d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)
        if norm_layer is not None:
            self.norm = norm_layer(embed_dim)
        else:
            self.norm = None

    def forward(self, x):
        """Forward function."""
        # padding
        
        _, _, D, H, W = x.size()
        if W % self.patch_size[2] != 0:
            x = F.pad(x, (0, self.patch_size[2] - W % self.patch_size[2]))
        if H % self.patch_size[1] != 0:
            x = F.pad(x, (0, 0, 0, self.patch_size[1] - H % self.patch_size[1]))
        if D % self.patch_size[0] != 0:
            x = F.pad(x, (0, 0, 0, 0, 0, self.patch_size[0] - D % self.patch_size[0]))

        x = self.proj(x)  # B C D Wh Ww
        if self.norm is not None:
            D, Wh, Ww = x.size(2), x.size(3), x.size(4)
            x = x.flatten(2).transpose(1, 2)
            x = self.norm(x)
            x = x.transpose(1, 2).view(-1, self.embed_dim, D, Wh, Ww)

        return x


class FusionModel(nn.Module):
    """
    structure: 4 encoding stages(BasicLayer) + 4 decoding stages(BasicLayerUp)
    Args:
        patch_size (int | tuple(int)): Patch size. Default: (4,4,4).
        in_chans (int): Number of input image channels. Default: 3.
        embed_dim (int): Number of linear projection output channels. Default: 96.
        depths (tuple[int]): Depths of each Swin Transformer stage.
        num_heads (tuple[int]): Number of attention head of each stage.
        window_size (int): Window size. Default: 7.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim. Default: 4.
        qkv_bias (bool): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float): Override default qk scale of head_dim ** -0.5 if set.
        drop_rate (float): Dropout rate.
        attn_drop_rate (float): Attention dropout rate. Default: 0.
        drop_path_rate (float): Stochastic depth rate. Default: 0.2.
        norm_layer: Normalization layer. Default: nn.LayerNorm.
        patch_norm (bool): If True, add normalization after patch embedding. Default: False.
        frozen_stages (int): Stages to be frozen (stop grad and set eval mode).
            -1 means not freezing any parameters.
    """

    def __init__(self,
                 pretrained=None,
                 pretrained2d=False,
                 patch_size=(4, 4, 4),
                 in_chans=1,
                 embed_dim=64,
                 depths=[2, 2, 6, 2],
                 num_heads=[3, 6, 12, 24],
                 window_size=(7, 7, 7),
                 mlp_ratio=4.,
                 qkv_bias=True,
                 qk_scale=None,
                 drop_rate=0.,
                 attn_drop_rate=0.,
                 drop_path_rate=0.2,
                 norm_layer=nn.LayerNorm,
                 patch_norm=False,
                 frozen_stages=-1
                 ):
        super().__init__()

        self.pretrained = pretrained
        self.pretrained2d = pretrained2d
        self.num_layers = len(depths)  # 4
        self.embed_dim = embed_dim  # 64
        self.patch_norm = patch_norm
        self.frozen_stages = frozen_stages
        self.window_size = window_size  # (7、7、7)
        self.patch_size = patch_size  # (4、4、4)

        # split image into non-overlapping patches
        self.patch_embed = PatchEmbed3D(
            patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim,
            norm_layer=norm_layer if self.patch_norm else None)

        self.pos_drop = nn.Dropout(p=drop_rate)

        # stochastic depth
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule

        # build layers
        self.layers = nn.ModuleList()
        for i_layer in range(self.num_layers):
            layer = BasicLayer(
                dim=int(embed_dim * 2 ** i_layer),  # dim=48\96\192\384
                depth=depths[i_layer],  # 2，2，6，2
                num_heads=num_heads[i_layer],  # [3, 6, 12, 24]
                window_size=window_size,  # [2,2,2]
                mlp_ratio=mlp_ratio,  # 4
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop_rate,  # 0
                attn_drop=attn_drop_rate,  # 0.2
                drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])],
                norm_layer=norm_layer,
                downsample=PatchMerging if i_layer < self.num_layers - 1 else None,  # 2*2*2卷积核 只在0、1、2下采样
                # downsample=None  # 2*2*2卷积核 只在0、1、2下采样
            )
            self.layers.append(layer)

        self.up_layers = nn.ModuleList()
        self.concat_back_dim = nn.ModuleList()
        for i_layer in reversed(range(self.num_layers)):
            concat_linear = nn.Linear(2 * int(embed_dim * 2 ** i_layer), int(embed_dim * 2 ** i_layer))

            up_layer = BasicLayer(
                dim=int(embed_dim * 2 ** i_layer),
                depth=depths[i_layer],
                num_heads=num_heads[i_layer],
                window_size=window_size,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop_rate,
                attn_drop=attn_drop_rate,
                drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])],
                norm_layer=norm_layer,
                # downsample=None
                downsample=PatchExpand if i_layer > 0 else None
            )
            self.up_layers.append(up_layer)
            self.concat_back_dim.append(concat_linear)

        self.num_features = int(embed_dim * 2 ** (self.num_layers - 1))

        # add a norm layer for each output
        self.norm = norm_layer(self.num_features)
        self.norm2 = norm_layer(self.embed_dim * 2)

        self.reverse_patch_embedding = nn.ConvTranspose3d(2 * embed_dim, embed_dim // 2, (4, 4, 4), stride=4)

    def forward(self, moving, fixed):
        """Forward function."""

        moving = self.patch_embed(moving)
        fixed = self.patch_embed(fixed)

        moving = self.pos_drop(moving)
        fixed = self.pos_drop(fixed)

        moving = rearrange(moving, 'n c d h w -> n d h w c')
        fixed = rearrange(fixed, 'n c d h w -> n d h w c')

        features_moving = []
        features_fixed = []
        for layer in self.layers:
            moving_out, fixed_out, moving, fixed = layer(moving.contiguous(), fixed.contiguous())
            features_moving.append(moving_out)
            features_fixed.append(fixed_out)

        moving = self.norm(moving)
        fixed = self.norm(fixed)

        for inx, layer_up in enumerate(self.up_layers):
            if inx == 0:
                _, _, moving, fixed = layer_up(moving, fixed)
            else:
                if moving.shape != features_moving[3 - inx].shape:
                    moving = rearrange(moving, 'n d h w c -> n c d h w')
                    fixed = rearrange(fixed, 'n d h w c -> n c d h w')
                    B, D, W, H, C = features_moving[3 - inx].shape
                    moving = F.interpolate(moving, size=(D, W, H), mode='trilinear', align_corners=True)
                    fixed = F.interpolate(fixed, size=(D, W, H), mode='trilinear', align_corners=True)
                    moving = rearrange(moving, 'n c d h w -> n d h w c')
                    fixed = rearrange(fixed, 'n c d h w -> n d h w c')

                moving = torch.cat([moving, features_moving[3 - inx]], -1)
                fixed = torch.cat([fixed, features_fixed[3 - inx]], -1)
                moving = self.concat_back_dim[inx](moving)
                fixed = self.concat_back_dim[inx](fixed)
                _, _, moving, fixed = layer_up(moving, fixed)

        x = torch.cat([moving, fixed], -1)
        x = self.norm2(x)
        x = rearrange(x, 'n d h w c -> n c d h w')

        x = self.reverse_patch_embedding(x)  # reverse the patch embedding to transfer the final feature into image size

        return x


class DC2Fusion(nn.Module):
    def __init__(self, n_channels=1 , embed_dim = 96 , window_size = (2,2,2),fusion_strategy="L1NW"):
        super().__init__()
        self.swin = FusionModel(window_size=window_size, in_chans=n_channels, embed_dim=embed_dim)
        self.out_conv = nn.Conv3d( embed_dim // 2, 2, 3, padding=1)
        self.fusion_strategy = fusion_strategy


    def forward(self, moving,fixed):

        x = self.swin(moving, fixed)  # fixed = (1,1,160,192,224)

        x = self.out_conv(x)
        x = fusion_strategy(x[:,0:1],x[:,1:2],device=x.device,strategy=self.fusion_strategy)

        return x


if __name__ == "__main__":
    moving = torch.zeros(size=(1,1,128, 128, 32)).cuda()
    fixed = torch.zeros_like(moving).cuda()
    model = DC2Fusion(n_channels=1 , embed_dim = 48).cuda()

    output = model(moving , fixed , None , None)
    print(output[0].shape)