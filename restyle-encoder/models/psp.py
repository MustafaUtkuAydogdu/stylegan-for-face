"""
This file defines the core research contribution
"""
import math
import torch
from torch import nn

# from models.stylegan2.model import Generator
from configs.paths_config import model_paths
from models.encoders import fpn_encoders, restyle_psp_encoders
from utils.model_utils import RESNET_MAPPING

from models.stylegan2_ada.generator import Generator as GeneratorAda
from models import gac


class pSp(nn.Module):

    def __init__(self, opts):
        super(pSp, self).__init__()
        self.set_opts(opts)
        self.n_styles = int(math.log(self.opts.output_size, 2)) * 2 - 2
        # Define architecture
        if not opts.generator_ada:
            self.decoder = Generator(self.opts.output_size, 512, 8, channel_multiplier=2)
        else:
            self.decoder = GeneratorAda(img_resolution=self.opts.output_size, z_dim=512, w_dim=512, img_channels=3, 
                                        w_num_layers=8)
                                        # w_num_layers=2)
            self.n_styles += 2    # temporary hardcoding for StyleGAN2-Ada; NOTE change might be required for a different resolution
        self.encoder = self.set_encoder()

        self.face_pool = torch.nn.AdaptiveAvgPool2d((256, 256))
        # Load weights if needed
        self.load_weights()

    def set_encoder(self):
        if self.opts.encoder_type == 'GradualStyleEncoder':
            encoder = fpn_encoders.GradualStyleEncoder(50, 'ir_se', self.n_styles, self.opts)
        elif self.opts.encoder_type == 'ResNetGradualStyleEncoder':
            encoder = fpn_encoders.ResNetGradualStyleEncoder(self.n_styles, self.opts)
        elif self.opts.encoder_type == 'BackboneEncoder':
            encoder = restyle_psp_encoders.BackboneEncoder(50, 'ir_se', self.n_styles, self.opts)
        elif self.opts.encoder_type == 'BackboneEncoder34':
            encoder = restyle_psp_encoders.BackboneEncoder(34, 'ir_se', self.n_styles, self.opts)
        elif self.opts.encoder_type == 'BackboneEncoder100':
            encoder = restyle_psp_encoders.BackboneEncoder(100, 'ir_se', self.n_styles, self.opts)
        elif self.opts.encoder_type == 'ResNetBackboneEncoder':
            encoder = restyle_psp_encoders.ResNetBackboneEncoder(self.n_styles, self.opts)
        else:
            raise Exception(f'{self.opts.encoder_type} is not a valid encoder')
        return encoder

    def load_weights(self):
        if self.opts.checkpoint_path is not None:
            print(f'Loading ReStyle pSp from checkpoint: {self.opts.checkpoint_path}')
            ckpt = torch.load(self.opts.checkpoint_path, map_location='cpu')
            self.encoder.load_state_dict(self.__get_keys(ckpt, 'encoder'), strict=False)
            self.decoder.load_state_dict(self.__get_keys(ckpt, 'decoder'), strict=True)
            self.__load_latent_avg(ckpt)
        else:
            encoder_ckpt = self.__get_encoder_checkpoint()
            if encoder_ckpt is not None:
                print('NOTE: weights not loaded (encoder ckpt not found)')
                print('NOTE: weights not loaded (encoder ckpt not found)')
                self.encoder.load_state_dict(encoder_ckpt, strict=False)
            print(f'Loading decoder weights from pretrained path: {self.opts.stylegan_weights}')
            ckpt = torch.load(self.opts.stylegan_weights)
            # self.decoder.load_state_dict(ckpt['g_ema'], strict=True)
            if not self.opts.generator_ada:
                self.decoder.load_state_dict(ckpt['g_ema'], strict=False)    # NOTE: fix for third-party stylegan2 generators
                                                                         # (see https://github.com/rosinality/stylegan2-pytorch/issues/71)
            else:
                #TODO filter keys
                g_state_dict = self.__get_keys(ckpt['state_dict'], 'G')
                self.decoder.load_state_dict(g_state_dict, strict=True)
            self.__load_latent_avg(ckpt, repeat=self.n_styles)

    def forward(self, x, latent=None, resize=True, latent_mask=None, input_code=False, randomize_noise=True,
                inject_latent=None, return_latents=False, alpha=None, average_code=False, input_is_full=False):
        if input_code:
            codes = x
        else:
            codes = self.encoder(x)
            # residual step
            if x.shape[1] == 6 and latent is not None:
                # learn error with respect to previous iteration
                codes = codes + latent
            else:
                # first iteration is with respect to the avg latent code
                codes = codes + self.latent_avg.repeat(codes.shape[0], 1, 1).to(codes.device)

        if latent_mask is not None:
            for i in latent_mask:
                if inject_latent is not None:
                    if alpha is not None:
                        codes[:, i] = alpha * inject_latent[:, i] + (1 - alpha) * codes[:, i]
                    else:
                        codes[:, i] = inject_latent[:, i]
                else:
                    codes[:, i] = 0

        if average_code:
            input_is_latent = True
        else:
            input_is_latent = (not input_code) or (input_is_full)

        images, result_latent = self.decoder([codes],
                                             input_is_latent=input_is_latent,
                                             randomize_noise=randomize_noise,
                                             return_latents=return_latents)

        if resize:
            images = self.face_pool(images)

        if return_latents:
            return images, result_latent
        else:
            return images

    def set_opts(self, opts):
        self.opts = opts

    def __load_latent_avg(self, ckpt, repeat=None):
        if 'latent_avg' in ckpt:
            self.latent_avg = ckpt['latent_avg'].to(self.opts.device)
            if repeat is not None:
                self.latent_avg = self.latent_avg.repeat(repeat, 1)
        else:
            self.latent_avg = None

    def __get_encoder_checkpoint(self):
        # if "ffhq" in self.opts.dataset_type:
        # if self.opts.encoder_type == 'BackboneEncoder':    # NOTE temporarily hardcoded
        #     if model_paths['ir_se50'] is None:
        #         return
        #     print('Loading encoders weights from irse50!')
        #     encoder_ckpt = torch.load(model_paths['ir_se50'])
        #     # Transfer the RGB input of the irse50 network to the first 3 input channels of pSp's encoder
        #     if self.opts.input_nc != 3:
        #         shape = encoder_ckpt['input_layer.0.weight'].shape
        #         altered_input_layer = torch.randn(shape[0], self.opts.input_nc, shape[2], shape[3], dtype=torch.float32)
        #         altered_input_layer[:, :3, :, :] = encoder_ckpt['input_layer.0.weight']
        #         encoder_ckpt['input_layer.0.weight'] = altered_input_layer
        #     return encoder_ckpt
        # else:    # this "if-else" is not so tested -- should be corrected for other encoder types
        #     return
        return

    @staticmethod
    def __get_keys(d, name):
        if 'state_dict' in d:
            d = d['state_dict']
        d_filt = {k[len(name) + 1:]: v for k, v in d.items() if k[:len(name)] == name}
        return d_filt
