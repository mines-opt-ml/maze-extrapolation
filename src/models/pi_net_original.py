"""
Based on deq_v3.py from "Path Independent Equilibrium Models Can Better Exploit Test-Time Computation" by Anil et. al.
"""

import pdb 
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
import torch.autograd as autograd
import numpy as np
import sys

from src.utils.device import get_device
from src.utils.pi_net.jacobian import jac_loss_estimate, power_method
from src.utils.pi_net.optimizations import weight_norm
from src.utils.pi_net.solvers import broyden

device = get_device()

class BasicBlock(nn.Module):
    """Basic residual block class"""
    expansion = 1

    def __init__(self, in_planes, planes, stride=1, in_channels=3, wnorm=False, norm_type='group'):
        super(BasicBlock, self).__init__()
        self.conv1 = nn.Conv2d(
            in_planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)

        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3,
                               stride=1, padding=1, bias=False)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != self.expansion*planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion*planes,
                          kernel_size=1, stride=stride, bias=False)
            )
        if wnorm: self._wnorm()

    def _wnorm(self):
        """
        Register weight normalization
        """
        self.conv1, self.conv1_fn = weight_norm(self.conv1, names=['weight'], dim=0)
        self.conv2, self.conv2_fn = weight_norm(self.conv2, names=['weight'], dim=0)
        if len(self.shortcut) > 0:
            self.shortcut[0].conv, self.shortcut_fn = weight_norm(self.shortcut[0].conv, names=['weight'], dim=0)
    
    def _reset(self, bsz, d, H, W):
        """
        Reset dropout mask and recompute weight via weight normalization
        """
        if 'conv1_fn' in self.__dict__:
            self.conv1_fn.reset(self.conv1)
        if 'conv2_fn' in self.__dict__:
            self.conv2_fn.reset(self.conv2)
        if 'shortcut_fn' in self.__dict__:
            self.shortcut_fn.reset(self.shortcut[0].conv)

    def forward(self, x, injection=None):
        # Move weights to device
        self.conv1.weight = self.conv1.weight.to(device)
        self.conv2.weight = self.conv2.weight.to(device)
        if len(self.shortcut) > 0:
            self.shortcut[0].weight = self.shortcut[0].weight.to(device)

        if injection is None: injection = 0
        out = self.conv1(x) + injection
        out = F.relu(out)
        out = self.conv2(out) 
        out += self.shortcut(x)
        out = F.relu(out)
        return out

blocks_dict = { 'BASIC': BasicBlock}

class DEQModule(nn.Module):
    def __init__(self, block, num_blocks, width, in_channels=3, norm_type='group'):
        super().__init__()
        self.in_planes = int(width)
        self.num_blocks = num_blocks 
        self.norm_type = norm_type
        self.recur_inj = nn.Conv2d(width + in_channels, width, kernel_size=3,
                                stride=1, padding=1, bias=False)
        self.recur_block = self._make_layer(block, width, num_blocks, stride=1)

    def _wnorm(self):
        """
        Apply weight normalization to the learnable parameters of MDEQ
        """
        self.recur_inj, self.recur_inj_fn = weight_norm(self.recur_inj, names=['weight'], dim=0)
        self.recur_inj.weight = self.recur_inj.weight.to(device)
        for block in self.recur_block:
            block._wnorm()        
        
        # Throw away garbage
        torch.cuda.empty_cache()
        
    def _reset(self, xs):
        """
        Reset the dropout mask and the learnable parameters (if weight normalization is applied)
        """

        if 'recur_inj_fn' in self.__dict__:
            self.recur_inj_fn.reset(self.recur_inj)

        for i, block in enumerate(self.recur_block):
            block._reset(*xs.shape)

    def _make_layer(self, block, planes, num_blocks, stride=1):
        """
        Make a specific branch indexed by `branch_index`. This branch contains `num_blocks` residual blocks of type `block`.
        """
        strides = [stride] + [1] * (num_blocks-1)
        layers = []
        for strd in strides:
            layers.append(block(self.in_planes, planes, strd)) #, norm_type=self.norm_type))
            self.in_planes = planes * block.expansion
        return nn.ModuleList(layers)

    def forward(self, x, x_init, injection=None):
        # Ensure weights are on device
        self.recur_inj.to(device)
        for block in self.recur_block:
            block.to(device)

        x = self.recur_inj(torch.cat([x, x_init], 1))
        for i in range(self.num_blocks):
            x = self.recur_block[i](x)
        return x

class DEQNet(nn.Module):
    """DeepThinking Network 2D model class"""

    def __init__(self, width, config, in_channels=3, **kwargs):
        super().__init__()
        self.kwargs = kwargs
        self.config = config
        self.parse_cfg(config)
        
        self.width = int(width)

        proj_conv = nn.Conv2d(in_channels, width, kernel_size=3,
                              stride=1, padding=1, bias=False)

        head_conv1 = nn.Conv2d(width, 32, kernel_size=3,
                               stride=1, padding=1, bias=False)
        head_conv2 = nn.Conv2d(32, 8, kernel_size=3,
                               stride=1, padding=1, bias=False)
        head_conv3 = nn.Conv2d(8, 2, kernel_size=3,
                               stride=1, padding=1, bias=False)

        self.projection = nn.Sequential(proj_conv, nn.ReLU())
        
        self.deq = DEQModule(blocks_dict[self.block_type], self.num_blocks, width)
        self.head = nn.Sequential(head_conv1, nn.ReLU(),
                                  head_conv2, nn.ReLU(),
                                  head_conv3)

        self.avg_iters = 0
        self.total_count = 0

        self.min_abs_trace = 0
        self.min_rel_trace = 0

        if self.wnorm:
            self.deq._wnorm()

    def parse_cfg(self, config):
        cfg = config['problem'] 
        # DEQ related
        if cfg['deq']['f_solver'] != 'fp_iters':
            self.f_solver = eval(cfg['deq']['f_solver'])
        else:
            self.f_solver = cfg['deq']['f_solver']

        self.b_solver = eval(cfg['deq']['b_solver'])
        if self.b_solver is None:
            self.b_solver = self.f_solver
        self.f_thres = cfg['deq']['f_thres']
        self.b_thres = cfg['deq']['b_thres']
        self.stop_mode = cfg['deq']['stop_mode']

        # Model related
        self.num_layers = cfg['deq']['num_layers']
        self.num_blocks = cfg['deq']['num_blocks']
        self.block_type = cfg['deq']['extra']['block']
        
        # Training related config
        self.pretrain_steps = cfg['train']['pretrain_steps']
        self.wnorm = cfg['deq']['wnorm']

        global NUM_GROUPS 
        NUM_GROUPS = cfg['deq']['num_groups']

        self.norm_type = cfg['deq']['norm']

        self.fp_init = cfg['deq']['fp_init']
        self.use_layer_loss = cfg['deq']['loss']['layer_loss']
        self.layer_idx = cfg['deq']['loss']['layer_idx']
        self.z_prev = None

        self.jacobian_factor = cfg['deq']['jacobian_factor']
        
    def forward(self, x, train_step=-1, 
                        interim_thought=None, 
                        iters_to_do=-1, 
                        return_interm_vals=False, 
                        compute_jac_loss=False, 
                        return_fp=False,
                        spectral_radius_mode=False,
                        return_residuals=False,
                        run_intervention=False,
                        **kwargs):

        self.use_jac_loss = compute_jac_loss
        jac_loss = torch.tensor(0.0).to(x)

        initial_thought = self.projection(x)
        
        deq_mode = (train_step < 0) or (train_step >= self.pretrain_steps)
        func = lambda z: self.deq(z, x)

        if interim_thought is None:
            z1 = initial_thought
        else:
            z1 = interim_thought

        #### run interventions
        if run_intervention:
            val = torch.rand(1)
            if val < 0.33:
                z1 = torch.zeros_like(z1)
            elif val >= 0.33 and val < 0.67:
                z1 = 2 * torch.rand_like(z1) - 1
                print("initializing between [-1, 1]")
            else:
                print("initializing to random proj")

        if self.fp_init == 'zeros':
            z1 = torch.zeros_like(z1)
        
        elif self.fp_init == 'random':
            z1 = 2 * torch.rand_like(z1) - 1

        if self.wnorm:
            self.deq._reset(x)
        
        fp_iters = self.num_layers
        if iters_to_do > 0:
            fp_iters = iters_to_do
            deq_mode = False

        if not deq_mode:
            rel_trace = []
            abs_trace = []
            interm_vals = []
            if return_interm_vals:
                steps_idx = np.arange(0, fp_iters, 10)

            for step in range(fp_iters):
                next_z1 = func(z1)
                abs_diff = (z1 - next_z1).norm()
                abs_trace.append(abs_diff.item())
                cur_rel_trace = abs_diff / (1e-5 + z1.norm())
                rel_trace.append(cur_rel_trace.item())
                z1 = next_z1
                if return_interm_vals and step in steps_idx:
                    interm_vals.append(next_z1)

            if (not self.training) and spectral_radius_mode:
                with torch.enable_grad():
                    new_z1 = func(z1.requires_grad_())
                _, e_large = power_method(new_z1, z1)
            new_z1 = z1

            self.min_abs_trace += min(abs_trace)
            self.min_rel_trace += min(rel_trace)
            self.total_count += 1
        else:
            interm_vals = []
            layer_idx = []
            if return_interm_vals:
                layer_idx = np.arange(0, self.f_thres, 10)

            with torch.no_grad():
                if self.f_solver == 'fp_iters':
                    rel_trace = []
                    abs_trace = []
                    if return_interm_vals:
                        steps_idx = np.arange(0, fp_iters, 10)

                    for step in range(fp_iters):
                        next_z1 = func(z1)
                        abs_diff = (z1 - next_z1).norm()
                        abs_trace.append(abs_diff.item())
                        cur_rel_trace = abs_diff / (1e-5 + z1.norm())
                        rel_trace.append(cur_rel_trace.item())
                        z1 = next_z1
                        if return_interm_vals and step in steps_idx:
                            interm_vals.append(next_z1)

                    self.total_count += 1
                    self.min_abs_trace += min(abs_trace)
                    self.min_rel_trace += min(rel_trace)
                else:   
                    if return_interm_vals:
                        layer_idx = np.arange(0, self.f_thres, 10)

                    result = self.f_solver(func, z1, threshold=self.f_thres, stop_mode=self.stop_mode, layer_idx=layer_idx, name="forward")
                    z1 = result['result']
                    self.avg_iters += result["nstep"]
                    self.total_count += 1
                    if return_interm_vals:
                        interm_vals = result["interm_vals"]

                    if train_step % 200 == 0 or train_step == -1:
                        print(f"[For] {train_step} {result['nstep']} {result['abs_trace']} {min(result['rel_trace'])}")
                    
                    abs_trace = result['abs_trace']
                    rel_trace = result['rel_trace']

                    self.min_abs_trace += min(result['abs_trace'])
                    self.min_rel_trace += min(result['rel_trace'])
            
            new_z1 = z1
            if (not self.training) and spectral_radius_mode:
                with torch.enable_grad():
                    new_z1 = func(z1.requires_grad_())
                _, e_large = power_method(new_z1, z1)

            if self.training:
                new_z1 = func(z1.requires_grad_())
                if self.use_jac_loss:
                    jac_loss = jac_loss_estimate(new_z1, z1)

                def backward_hook(grad):
                    if self.hook is not None:
                        self.hook.remove()
                        torch.cuda.synchronize()
                    result = self.b_solver(lambda y: autograd.grad(new_z1, z1, y, retain_graph=True)[0] + grad, torch.zeros_like(grad), 
                                          threshold=self.b_thres, stop_mode=self.stop_mode, name="backward")

                    if train_step % 200 == 0:
                        print(f"[Back] {train_step} {result['nstep']} {min(result['abs_trace'])} {min(result['rel_trace'])}")
                    return self.jacobian_factor * result['result']

                self.hook = new_z1.register_hook(backward_hook)

        thought = self.head(new_z1)

        if self.use_jac_loss:
            jac_loss = jac_loss.view(1, -1)
        
        if self.training:
            if compute_jac_loss:
                return thought, jac_loss
            return thought, new_z1
        
        if spectral_radius_mode:
            return thought, e_large

        if return_fp:
            if return_residuals:
                if return_interm_vals:
                    return thought, new_z1, abs_trace, interm_vals
                return thought, new_z1, abs_trace
            return thought, new_z1

        if return_residuals:
            return thought, abs_trace, rel_trace

        return thought