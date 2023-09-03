#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File      : onnx2trt.py
@Time      : 2023/05/12 14:41:23
@Author    : Huang Bo
@Contact   : cenahwang0304@gmail.com
@Desc      : None
'''


import torch
from torch.nn import functional as F
import numpy as np
from torchvision.transforms.functional import resize, to_pil_image
from typing import Tuple
import cv2
import matplotlib.pyplot as plt
import warnings
import os
os.environ["KMP_DUPLICATE_LIB_OK"]  =  "TRUE"
from pathlib import Path
import tensorrt as trt
import argparse
from segment_anything import sam_model_registry
from segment_anything.utils.onnx import SamOnnxModel

def show_mask(mask, ax, random_color=False):
    if random_color:
        color = np.concatenate([np.random.random(3), np.array([0.6])], axis=0)
    else:
        color = np.array([30/255, 144/255, 255/255, 0.6])
    h, w = mask.shape[-2:]
    mask_image = mask.reshape(h, w, 1) * color.reshape(1, 1, -1)
    ax.imshow(mask_image)
    
def show_points(coords, labels, ax, marker_size=375):
    pos_points = coords[labels==1]
    neg_points = coords[labels==0]
    ax.scatter(pos_points[:, 0], pos_points[:, 1], color='green', marker='*', s=marker_size, edgecolor='white', linewidth=1.25)
    ax.scatter(neg_points[:, 0], neg_points[:, 1], color='red', marker='*', s=marker_size, edgecolor='white', linewidth=1.25)   
    
def show_box(box, ax):
    x0, y0 = box[0], box[1]
    w, h = box[2] - box[0], box[3] - box[1]
    ax.add_patch(plt.Rectangle((x0, y0), w, h, edgecolor='green', facecolor=(0,0,0,0), lw=2))  

def get_preprocess_shape(oldh: int, oldw: int, long_side_length: int) -> Tuple[int, int]:
    """
    Compute the output size given input size and target long side length.
    """
    scale = long_side_length * 1.0 / max(oldh, oldw)
    newh, neww = oldh * scale, oldw * scale
    neww = int(neww + 0.5)
    newh = int(newh + 0.5)
    return (newh, neww)

def pre_processing(image: np.ndarray, target_length: int, device,pixel_mean,pixel_std,img_size):
    target_size = get_preprocess_shape(image.shape[0], image.shape[1], target_length)
    input_image = np.array(resize(to_pil_image(image), target_size))
    input_image_torch = torch.as_tensor(input_image, device=device)
    input_image_torch = input_image_torch.permute(2, 0, 1).contiguous()[None, :, :, :]

    # Normalize colors
    input_image_torch = (input_image_torch - pixel_mean) / pixel_std

    # Pad
    h, w = input_image_torch.shape[-2:]
    padh = img_size - h
    padw = img_size - w
    input_image_torch = F.pad(input_image_torch, (0, padw, 0, padh))
    return input_image_torch

def export_embedding_model(gpu_id, model_type, sam_checkpoint, opset):
    device = f"cuda:{gpu_id}"
    sam = sam_model_registry[model_type](checkpoint=sam_checkpoint)
    sam.to(device=device)

    image = cv2.imread('./images/truck.jpg')
    target_length = sam.image_encoder.img_size
    pixel_mean = sam.pixel_mean 
    pixel_std = sam.pixel_std
    img_size = sam.image_encoder.img_size
    inputs = pre_processing(image, target_length, device, pixel_mean, pixel_std, img_size)
    os.makedirs("embedding_onnx", exist_ok=True)
    onnx_model_path = os.path.join("embedding_onnx", "sam_" + model_type+"_"+"embedding.onnx")
    dummy_inputs = {"images": inputs}

    output_names = ["image_embeddings"]
    # image_embeddings = sam.image_encoder(inputs).cpu().numpy()
    # print('image_embeddings', image_embeddings.shape)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=torch.jit.TracerWarning)
        warnings.filterwarnings("ignore", category=UserWarning)
        # with open(onnx_model_path, "wb") as f:
        torch.onnx.export(
            sam.image_encoder,
            tuple(dummy_inputs.values()),
            onnx_model_path,
            export_params=True,
            verbose=False,
            opset_version=opset,
            do_constant_folding=True,
            input_names=list(dummy_inputs.keys()),
            output_names=output_names,
        )  
    print(f"Generate image onnx model, and save in: {onnx_model_path}")  

def export_prompt_masks_model(model_type: str, checkpoint: str, opset: int):
    print("Loading model...")
    sam = sam_model_registry[model_type](checkpoint=checkpoint)

    onnx_model = SamOnnxModel(
        model=sam,
        return_single_mask=True,
        use_stability_score=False,
        return_extra_metrics=False,
    )
    onnx_model_path = os.path.join("weights", "sam_" + model_type+"_"+"prompt_mask.onnx")
    
    dynamic_axes = {
        "point_coords": {1: "num_points"},
        "point_labels": {1: "num_points"},
    }

    embed_dim = sam.prompt_encoder.embed_dim
    embed_size = sam.prompt_encoder.image_embedding_size
    mask_input_size = [4 * x for x in embed_size]
    dummy_inputs = {
        "image_embeddings": torch.randn(1, embed_dim, *embed_size, dtype=torch.float),
        "point_coords": torch.randint(low=0, high=1024, size=(1, 5, 2), dtype=torch.float),
        "point_labels": torch.randint(low=0, high=4, size=(1, 5), dtype=torch.float),
        "mask_input": torch.randn(1, 1, *mask_input_size, dtype=torch.float),
        "has_mask_input": torch.tensor([1], dtype=torch.float),
        # "orig_im_size": torch.tensor([1500, 2250], dtype=torch.int32),
    }

    _ = onnx_model(**dummy_inputs)

    output_names = ["low_res_masks", "iou_predictions"]

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=torch.jit.TracerWarning)
        warnings.filterwarnings("ignore", category=UserWarning)
        with open(onnx_model_path, "wb") as f:
            print(f"Exporting onnx model to {onnx_model_path}...")
            torch.onnx.export(
                onnx_model,
                tuple(dummy_inputs.values()),
                f,
                export_params=True,
                verbose=False,
                opset_version=opset,
                do_constant_folding=True,
                input_names=list(dummy_inputs.keys()),
                output_names=output_names,
                dynamic_axes=dynamic_axes,
            )
    print(f"Generate prompt and masks onnx model, and save in: {onnx_model_path}") 

def export_prompt_model(gpu_id=1, model_type="default", sam_checkpoint="weights/sam_vit_h_4b8939.pth"):
    device = f"cuda:{gpu_id}"
    sam = sam_model_registry[model_type](checkpoint=sam_checkpoint)
    sam.to(device=device)

    os.makedirs("prompt_onnx", exist_ok=True)
    
    embed_size = sam.prompt_encoder.image_embedding_size
    mask_input_size = [4 * x for x in embed_size]
    onnx_model_path = os.path.join("prompt_onnx", "sam_" + model_type+"_"+"prompt.onnx")
    dynamic_axes = {
        "point_coords": {0: "num_points"},
        "point_labels": {0: "num_points"},
        "boxes": {0: "num_boxes"},
    }
    points_coord = torch.randint(low=0, high=1024, size=(1, 1, 2), dtype=torch.float).to(device)
    points_label = torch.randint(low=0, high=4, size=(1, 1), dtype=torch.float).to(device)
    points = (points_coord, points_label)
    boxes = torch.randint(low=0, high=1024, size=(1, 1, 4), dtype=torch.int32).to(device)
    
    dummy_inputs = { 
        "points": points,
        "boxes": boxes,
        "masks": torch.randn(1, 1, *mask_input_size, dtype=torch.float).to(device),
        }
    input_names = ["point_coords", "point_labels", "boxes", "mask_input"]

    output_names = ["sparse_embeddings", "dense_embeddings"]  
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=torch.jit.TracerWarning)
        warnings.filterwarnings("ignore", category=UserWarning)
        # with open(onnx_model_path, "wb") as f:
        torch.onnx.export(
            sam.prompt_encoder,
            tuple(dummy_inputs.values()),
            onnx_model_path,
            export_params=True,
            verbose=False,
            opset_version=17,
            do_constant_folding=True,
            input_names=input_names,
            output_names=output_names,
            dynamic_axes=dynamic_axes,
        )  
    print(f"Generate image onnx model, and save in: {onnx_model_path}")     

def export_masks_model(gpu_id=2, model_type="default", sam_checkpoint="weights/sam_vit_h_4b8939.pth"):
    device = f"cuda:{gpu_id}"
    sam = sam_model_registry[model_type](checkpoint=sam_checkpoint)
    sam.to(device=device)

    os.makedirs("masks_onnx", exist_ok=True)
    
    onnx_model_path = os.path.join("masks_onnx", "sam_" + model_type+"_"+"masks.onnx")
    dynamic_axes = {
        "sparse_embeddings": {1: "num_embedding"},
    }
    sparse_embeddings = torch.randint(low=0, high=1024, size=(1, 2, 256), dtype=torch.float).to(device)
    dense_embeddings = torch.randint(low=0, high=1024, size=(1, 256, 64, 64), dtype=torch.float).to(device)
    image_embeddings = torch.randint(low=0, high=1024, size=(1, 256, 64, 64), dtype=torch.int32).to(device)
    image_pe = torch.randint(low=0, high=1024, size=(1, 256, 64, 64), dtype=torch.int32).to(device)
    multimask_output = torch.tensor([0], dtype=torch.float).to(device)
    
    dummy_inputs = { 
        "image_embeddings": image_embeddings,
        "image_pe": image_pe,
        "sparse_embeddings": sparse_embeddings,
        "dense_embeddings": dense_embeddings,
        "multimask_output": multimask_output,
        }

    output_names = ["low_res_masks", "iou_predictions"]  
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=torch.jit.TracerWarning)
        warnings.filterwarnings("ignore", category=UserWarning)
        # with open(onnx_model_path, "wb") as f:
        torch.onnx.export(
            sam.mask_decoder,
            tuple(dummy_inputs.values()),
            onnx_model_path,
            export_params=True,
            verbose=False,
            opset_version=17,
            do_constant_folding=True,
            input_names=list(dummy_inputs.keys()),
            output_names=output_names,
            dynamic_axes=dynamic_axes,
        )  
    print(f"Generate image onnx model, and save in: {onnx_model_path}")     
   
def export_engine_image_encoder(f='vit_l_embedding.onnx', half=True):
    file = Path(f)
    f = file.with_suffix('.engine')  # TensorRT engine file
    onnx = file.with_suffix('.onnx')
    logger = trt.Logger(trt.Logger.INFO)
    builder = trt.Builder(logger)
    config = builder.create_builder_config()
    workspace = 6
    print("workspace: ", workspace)
    config.max_workspace_size = workspace * 1 << 30
    flag = (1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
    network = builder.create_network(flag)
    parser = trt.OnnxParser(network, logger)
    if not parser.parse_from_file(str(onnx)):
        raise RuntimeError(f'failed to load ONNX file: {onnx}')

    inputs = [network.get_input(i) for i in range(network.num_inputs)]
    outputs = [network.get_output(i) for i in range(network.num_outputs)]
    for inp in inputs:
        print(f'input "{inp.name}" with shape{inp.shape} {inp.dtype}')
    for out in outputs:
        print(f'output "{out.name}" with shape{out.shape} {out.dtype}')

    print(f'building FP{16 if builder.platform_has_fast_fp16 and half else 32} engine as {f}')
    if builder.platform_has_fast_fp16 and half:
        config.set_flag(trt.BuilderFlag.FP16)
    
    with builder.build_engine(network, config) as engine, open(f, 'wb') as t:
        t.write(engine.serialize())
    print(f"Generate image embedding trt model, save in: {f}")

def export_engine_prompt_encoder_and_mask_decoder(f='sam_onnx_example.onnx', half=True):
    import tensorrt as trt
    from pathlib import Path
    file = Path(f)
    f = file.with_suffix('.engine')  # TensorRT engine file
    onnx = file.with_suffix('.onnx')
    logger = trt.Logger(trt.Logger.INFO)
    builder = trt.Builder(logger)
    config = builder.create_builder_config()
    workspace = 6
    print("workspace: ", workspace)
    config.max_workspace_size = workspace * 1 << 30
    flag = (1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
    network = builder.create_network(flag)
    parser = trt.OnnxParser(network, logger)
    print(str(onnx))
    if not parser.parse_from_file(str(onnx)):
        raise RuntimeError(f'failed to load ONNX file: {onnx}')

    inputs = [network.get_input(i) for i in range(network.num_inputs)]
    outputs = [network.get_output(i) for i in range(network.num_outputs)]
    for inp in inputs:
        print(f'input "{inp.name}" with shape{inp.shape} {inp.dtype}')
    for out in outputs:
        print(f'output "{out.name}" with shape{out.shape} {out.dtype}')

    profile = builder.create_optimization_profile()
    profile.set_shape('image_embeddings', (1, 256, 64, 64), (1, 256, 64, 64), (1, 256, 64, 64))
    profile.set_shape('point_coords', (1, 2,2), (1, 5,2), (1,10,2))
    profile.set_shape('point_labels', (1, 2), (1, 5), (1,10))
    profile.set_shape('mask_input', (1, 1, 256, 256), (1, 1, 256, 256), (1, 1, 256, 256))
    profile.set_shape('has_mask_input', (1,), (1, ), (1, ))
    # profile.set_shape_input('orig_im_size', (1200, 1800), (1200, 1800), (1200, 1800)) # Must be consistent with input
    config.add_optimization_profile(profile)

    print(f'building FP{16 if builder.platform_has_fast_fp16 and half else 32} engine as {f}')
    if builder.platform_has_fast_fp16 and half:
        config.set_flag(trt.BuilderFlag.FP16)
    with builder.build_engine(network, config) as engine, open(f, 'wb') as t:
        t.write(engine.serialize())


if __name__ == '__main__':
    parser = argparse.ArgumentParser("transform pth model to onnx, or transform onnx to tensorrt")
    parser.add_argument("--img_pt2onnx", action="store_true", help="transform image embedding pth from sam model to onnx")
    parser.add_argument("--sam_checkpoint", type=str, default="/root/.cache/ckpts/sam_model_vit_l.pth")
    parser.add_argument("--model_type", type=str, default="vit_l")
    parser.add_argument("--prompt_masks_pt2onnx", action="store_true", help="whether export prompt encoder and masks decoder module")
    parser.add_argument("--img_onnx2trt", action="store_true", help="only transform image embedding onnx model to tensorrt engine")
    parser.add_argument("--img_onnx_model_path", type=str, default="embedding_onnx/sam_default_embedding.onnx")
    parser.add_argument("--sam_onnx2trt", action="store_true", help="only transform sam prompt and mask decoder onnx model to tensorrt engine")
    parser.add_argument("--sam_onnx_path", type=str, default="./weights/sam_vit_h_4b8939.onnx")
    parser.add_argument("--gpu_id", type=int, default=0, help="use which gpu to transform model")
    parser.add_argument("--opset", type=int, default=17, help="onnx opset version")
    args = parser.parse_args()

    with torch.no_grad():
        if args.img_pt2onnx:
            export_embedding_model(args.gpu_id, args.model_type, args.sam_checkpoint, args.opset)
        if args.prompt_masks_pt2onnx:
            export_prompt_masks_model(args.model_type, args.sam_checkpoint, args.opset)
        if args.img_onnx2trt:
            export_engine_image_encoder(args.img_onnx_model_path, False)
        if args.sam_onnx2trt:
            export_engine_prompt_encoder_and_mask_decoder(args.sam_onnx_path)
           
        # just test split prompt encoder and masks decoder module 
        # export_prompt_model()
        # export_masks_model()