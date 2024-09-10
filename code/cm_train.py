"""
Train a diffusion model on images.
"""
import argparse
from operator import is_

from cm import dist_util, logger
# from cm.image_datasets import load_data
from cm.ddbm_datasets import load_data
from cm.script_util import (
    train_defaults,
    model_and_diffusion_defaults,
    create_model_and_diffusion,
    cm_train_defaults,
    ctm_train_defaults,
    ctm_eval_defaults,
    ctm_loss_defaults,
    ctm_data_defaults,
    add_dict_to_argparser,
    create_ema_and_scales_fn,
)
from cm.train_util import CMTrainLoop
import torch.distributed as dist
import copy
import cm.enc_dec_lib as enc_dec_lib
import torch as th

def main():
    
    args = create_argparser().parse_args()
    if args.use_MPI:
        dist_util.setup_dist(args.device_id)
    else:
        dist_util.setup_dist_without_MPI(args.device_id)

    logger.configure(args, dir=args.out_dir)

    logger.log("Creating data loader...")
    if args.batch_size == -1:
        batch_size = args.global_batch_size // dist.get_world_size()
        if args.global_batch_size % dist.get_world_size() != 0:
            logger.log(
                f"warning, using smaller global_batch_size of {dist.get_world_size() * batch_size} instead of {args.global_batch_size}"
            )
    else:
        batch_size = args.batch_size

    augment = None
    if args.data_name.startswith('edges2'):
        from datasets.augment import AugmentPipe
        augment = AugmentPipe(
                    p=0.12,xflip=1e8, yflip=1, scale=1, rotate_frac=1, aniso=1, translate_frac=1
                )
        augment = None
        
        data_image_size = args.image_size
        batch_size = args.batch_size
        
        # data_image_size = 64
        # batch_size = 16
        
        perform_test = True
        data, test_data = load_data(
            data_dir=args.data_dir,
            data_name=args.data_name,
            batch_size=batch_size,
            image_size=data_image_size,
            num_workers=args.num_workers,
        )
    else:
        perform_test = False
        test_data = None
        data = load_data(
            args=args,
            data_name=args.data_name,
            data_dir=args.data_dir,
            batch_size=batch_size,
            image_size=args.image_size,
            class_cond=args.class_cond,
            train_classes=args.train_classes,
            num_workers=args.num_workers,
            type=args.type,
            deterministic=args.deterministic,
        )

    logger.log("Creating model and diffusion...")
    ema_scale_fn = create_ema_and_scales_fn(
        target_ema_mode=args.target_ema_mode,
        start_ema=args.start_ema,
        scale_mode=args.scale_mode,
        start_scales=args.start_scales,
        end_scales=args.end_scales,
        total_steps=args.total_training_steps,
        distill_steps_per_iter=args.distill_steps_per_iter,
    )

    # Load Feature Extractor
    feature_extractor = enc_dec_lib.load_feature_extractor(args, eval=True)
    # Load Discriminator
    discriminator, discriminator_feature_extractor = enc_dec_lib.load_discriminator_and_d_feature_extractor(args)
    # Load Model
    model, diffusion = create_model_and_diffusion(args, feature_extractor, discriminator_feature_extractor)
    model.to(dist_util.dev())
    model.train()
    if args.use_fp16:
        model.convert_to_fp16()
    elif args.use_bf16:
        model.convert_to_bf16()

    if len(args.teacher_model_path) > 0 and not args.self_learn:  # path to the teacher score model.
        # print("Should not happen, since no teacher model is used.")
        # exit()
        logger.log(f"Loading the teacher model from {args.teacher_model_path}.")
        teacher_model, _ = create_model_and_diffusion(args, teacher=True)
        if not args.edm_nn_ncsn and not args.edm_nn_ddpm:
            teacher_model.load_state_dict(
                dist_util.load_state_dict(args.teacher_model_path, map_location=dist_util.dev()),
            )
        teacher_model.to(dist_util.dev())
        teacher_model.eval()

        def filter_(dst_name):
            dst_ = dst_name.split('.')
            for idx, name in enumerate(dst_):
                if '_train' in name:
                    dst_[idx] = ''.join(name.split('_train'))
            return '.'.join(dst_)

        for dst_name, dst in model.named_parameters():
            for src_name, src in teacher_model.named_parameters():
                if dst_name in ['.'.join(src_name.split('.')[1:]), src_name]:
                    dst.data.copy_(src.data)
                    if args.linear_probing:
                        dst.requires_grad = False
                    break
                if args.linear_probing:
                    if filter_(dst_name) in ['.'.join(src_name.split('.')[1:]), src_name]:
                        dst.data.copy_(src.data)
                        break
        teacher_model.requires_grad_(False)
        if args.edm_nn_ncsn:
            model.model.map_noise.freqs = teacher_model.model.model.map_noise.freqs
        if args.use_fp16:
            teacher_model.convert_to_fp16()
        elif args.use_bf16:
            teacher_model.convert_to_bf16()
    else:
        teacher_model = None
    
    if args.self_learn:
        assert teacher_model is None
    
    # load the target model for distillation, if path specified.

    # if args.start_ema == 0. and args.scale_mode == 'ict_exp':
    #     logger.log("Target model is going to be equal to the student model (acc. to the iCT paper)!")
    #     target_model = None
    # else:
    logger.log("creating the target model (ie. ema model)")
    target_model, _ = create_model_and_diffusion(args) # = ema model

    target_model.to(dist_util.dev())
    target_model.train()

    # is_nan = th.stack([th.isnan(p).any() for p in target_model.parameters()]).any()
    # print('isnan', is_nan)
    # exit()

    dist_util.sync_params(target_model.parameters())
    dist_util.sync_params(target_model.buffers())

    for dst, src in zip(target_model.parameters(), model.parameters()):
        dst.data.copy_(src.data)

    if args.use_fp16:
        target_model.convert_to_fp16()
    elif args.use_bf16:
        target_model.convert_to_bf16()
    if args.edm_nn_ncsn:
        target_model.model.map_noise.freqs = teacher_model.model.model.map_noise.freqs

    is_I2I: bool = args.is_I2I
    logger.log(f"Training {'Image' if is_I2I else 'Noise'}-to-Image...")
    
    CMTrainLoop(
        model=model, #< model learns the "score function".
        target_model=target_model,
        teacher_model=teacher_model, #< teacher model is used to learn the "Paths"
        discriminator=discriminator,
        ema_scale_fn=ema_scale_fn,
        diffusion=diffusion,
        data=data,
        batch_size=batch_size,
        args=args,
        augment=augment,
        test_data=test_data,
        perform_test=perform_test,
        is_I2I=is_I2I,
    ).run_loop()

def create_argparser():
    
    # defaults = dict(data_name='cifar10')
    defaults = dict(data_name='edges2handbags')
    # defaults = dict(data_name='imagenet64')
    defaults.update(train_defaults(defaults['data_name']))
    defaults.update(ctm_train_defaults(defaults['data_name']))
    defaults.update(model_and_diffusion_defaults(defaults['data_name'], defaults['is_I2I']))
    defaults.update(cm_train_defaults(defaults['data_name']))
    defaults.update(ctm_eval_defaults(defaults['data_name']))
    defaults.update(ctm_loss_defaults(defaults['data_name']))
    defaults.update(ctm_data_defaults(defaults['data_name']))
    defaults.update()
    parser = argparse.ArgumentParser()
    add_dict_to_argparser(parser, defaults)
    return parser


if __name__ == "__main__":

    main()
