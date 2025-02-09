# YOLOv5 🚀 by Ultralytics, GPL-3.0 license
"""
Export a YOLOv5 PyTorch model to other formats. TensorFlow exports authored by https://github.com/zldrobit

Format                      | `export.py --include`         | Model
---                         | ---                           | ---
PyTorch                     | -                             | yolov5s.pt
TorchScript                 | `torchscript`                 | yolov5s.torchscript
ONNX                        | `onnx`                        | yolov5s.onnx
OpenVINO                    | `openvino`                    | yolov5s_openvino_model/
TensorRT                    | `engine`                      | yolov5s.engine
CoreML                      | `coreml`                      | yolov5s.mlmodel
TensorFlow SavedModel       | `saved_model`                 | yolov5s_saved_model/
TensorFlow GraphDef         | `pb`                          | yolov5s.pb
TensorFlow Lite             | `tflite`                      | yolov5s.tflite
TensorFlow Edge TPU         | `edgetpu`                     | yolov5s_edgetpu.tflite
TensorFlow.js               | `tfjs`                        | yolov5s_web_model/
PaddlePaddle                | `paddle`                      | yolov5s_paddle_model/

Requirements:
    $ pip install -r requirements.txt coremltools onnx onnx-simplifier onnxruntime openvino-dev tensorflow-cpu  # CPU
    $ pip install -r requirements.txt coremltools onnx onnx-simplifier onnxruntime-gpu openvino-dev tensorflow  # GPU

Usage:
    $ python export.py --weights yolov5s.pt --include torchscript onnx openvino engine coreml tflite ...

Inference:
    $ python detect.py --weights yolov5s.pt                 # PyTorch
                                 yolov5s.torchscript        # TorchScript
                                 yolov5s.onnx               # ONNX Runtime or OpenCV DNN with --dnn
                                 yolov5s_openvino_model     # OpenVINO
                                 yolov5s.engine             # TensorRT
                                 yolov5s.mlmodel            # CoreML (macOS-only)
                                 yolov5s_saved_model        # TensorFlow SavedModel
                                 yolov5s.pb                 # TensorFlow GraphDef
                                 yolov5s.tflite             # TensorFlow Lite
                                 yolov5s_edgetpu.tflite     # TensorFlow Edge TPU
                                 yolov5s_paddle_model       # PaddlePaddle

TensorFlow.js:
    $ cd .. && git clone https://github.com/zldrobit/tfjs-yolov5-example.git && cd tfjs-yolov5-example
    $ npm install
    $ ln -s ../../yolov5/yolov5s_web_model public/yolov5s_web_model
    $ npm start
"""

import argparse
import contextlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import warnings
from pathlib import Path


# activate rknn hack
if '--rknpu' in sys.argv:
    os.environ['RKNN_model_hack'] = "1"
    rknpu = True
else:
    rknpu = False

import pandas as pd
import torch

FILE = Path(__file__).resolve()
ROOT = FILE.parents[0]  # YOLOv5 root directory
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # add ROOT to PATH
if platform.system() != 'Windows':
    ROOT = Path(os.path.relpath(ROOT, Path.cwd()))  # relative

from models.experimental import attempt_load
from models.yolo import ClassificationModel, Detect, DetectionModel, SegmentationModel, Segment
from utils.dataloaders import LoadImages
from utils.general import (LOGGER, Profile, check_dataset, check_img_size, check_version,
                           check_yaml, colorstr, file_size, get_default_args, print_args, url2file, yaml_save)
from utils.torch_utils import select_device, smart_inference_mode

MACOS = platform.system() == 'Darwin'  # macOS environment


def export_formats():
    # YOLOv5 export formats
    x = [
        ['ONNX', 'onnx', '.onnx', True, True],
        ['OpenVINO', 'openvino', '_openvino_model', True, False],
        ['TensorRT', 'engine', '.engine', False, True],
        ['RKNN', 'rknn', '.rknn', True, False],
    ]
    return pd.DataFrame(x, columns=['Format', 'Argument', 'Suffix', 'CPU', 'GPU'])


def try_export(inner_func):
    # YOLOv5 export decorator, i..e @try_export
    inner_args = get_default_args(inner_func)

    def outer_func(*args, **kwargs):
        prefix = inner_args['prefix']
        try:
            with Profile() as dt:
                f, model = inner_func(*args, **kwargs)
            LOGGER.info(f'{prefix} export success ✅ {dt.t:.1f}s, saved as {f} ({file_size(f):.1f} MB)')
            return f, model
        except Exception as e:
            LOGGER.info(f'{prefix} export failure ❌ {dt.t:.1f}s: {e}')
            return None, None

    return outer_func


@try_export
def export_onnx(model, im, file, opset, dynamic, simplify, prefix=colorstr('ONNX:')):
    # YOLOv5 ONNX export
    import onnx

    LOGGER.info(f'\n{prefix} starting export with onnx {onnx.__version__}...')
    f = file.with_suffix('.onnx')

    output_names = ['output0', 'output1'] if isinstance(model, SegmentationModel) else ['output0']
    if dynamic:
        dynamic = {'images': {0: 'batch', 2: 'height', 3: 'width'}}  # shape(1,3,640,640)
        if isinstance(model, SegmentationModel):
            dynamic['output0'] = {0: 'batch', 1: 'anchors'}  # shape(1,25200,85)
            dynamic['output1'] = {0: 'batch', 2: 'mask_height', 3: 'mask_width'}  # shape(1,32,160,160)
        elif isinstance(model, DetectionModel):
            dynamic['output0'] = {0: 'batch', 1: 'anchors'}  # shape(1,25200,85)

    torch.onnx.export(
        model.cpu() if dynamic else model,  # --dynamic only compatible with cpu
        im.cpu() if dynamic else im,
        f,
        verbose=False,
        opset_version=opset,
        do_constant_folding=True,  # WARNING: DNN inference with torch>=1.12 may require do_constant_folding=False
        input_names=['images'],
        output_names=output_names,
        dynamic_axes=dynamic or None)

    # Checks
    model_onnx = onnx.load(f)  # load onnx model
    onnx.checker.check_model(model_onnx)  # check onnx model

    # Metadata
    d = {'stride': int(max(model.stride)), 'names': model.names}
    for k, v in d.items():
        meta = model_onnx.metadata_props.add()
        meta.key, meta.value = k, str(v)
    onnx.save(model_onnx, f)

    # Simplify
    if simplify:
        try:
            cuda = torch.cuda.is_available()
            import onnxsim

            LOGGER.info(f'{prefix} simplifying with onnx-simplifier {onnxsim.__version__}...')
            model_onnx, check = onnxsim.simplify(model_onnx)
            assert check, 'assert check failed'
            onnx.save(model_onnx, f)
        except Exception as e:
            LOGGER.info(f'{prefix} simplifier failure: {e}')
    return f, model_onnx


@try_export
def export_openvino(file, metadata, half, prefix=colorstr('OpenVINO:')):
    # YOLOv5 OpenVINO export
    import openvino.inference_engine as ie

    LOGGER.info(f'\n{prefix} starting export with openvino {ie.__version__}...')
    f = str(file).replace('.pt', f'_openvino_model{os.sep}')

    args = [
        'mo',
        '--input_model',
        str(file.with_suffix('.onnx')),
        '--output_dir',
        f,
        '--data_type',
        ('FP16' if half else 'FP32'),]
    subprocess.run(args, check=True, env=os.environ)  # export
    yaml_save(Path(f) / file.with_suffix('.yaml').name, metadata)  # add metadata.yaml
    return f, None


@try_export
def export_engine(model, im, file, half, dynamic, simplify, workspace=4, verbose=False, prefix=colorstr('TensorRT:')):
    # YOLOv5 TensorRT export https://developer.nvidia.com/tensorrt
    assert im.device.type != 'cpu', 'export running on CPU but must be on GPU, i.e. `python export.py --device 0`'
    try:
        import tensorrt as trt
    except Exception:
        import tensorrt as trt

    if trt.__version__[0] == '7':  # TensorRT 7 handling https://github.com/ultralytics/yolov5/issues/6012
        grid = model.model[-1].anchor_grid
        model.model[-1].anchor_grid = [a[..., :1, :1, :] for a in grid]
        export_onnx(model, im, file, 12, dynamic, simplify)  # opset 12
        model.model[-1].anchor_grid = grid
    else:  # TensorRT >= 8
        check_version(trt.__version__, '8.0.0', hard=True)  # require tensorrt>=8.0.0
        export_onnx(model, im, file, 12, dynamic, simplify)  # opset 12
    onnx = file.with_suffix('.onnx')

    LOGGER.info(f'\n{prefix} starting export with TensorRT {trt.__version__}...')
    assert onnx.exists(), f'failed to export ONNX file: {onnx}'
    f = file.with_suffix('.engine')  # TensorRT engine file
    logger = trt.Logger(trt.Logger.INFO)
    if verbose:
        logger.min_severity = trt.Logger.Severity.VERBOSE

    builder = trt.Builder(logger)
    config = builder.create_builder_config()
    config.max_workspace_size = workspace * 1 << 30
    # config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, workspace << 30)  # fix TRT 8.4 deprecation notice

    flag = (1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
    network = builder.create_network(flag)
    parser = trt.OnnxParser(network, logger)
    if not parser.parse_from_file(str(onnx)):
        raise RuntimeError(f'failed to load ONNX file: {onnx}')

    inputs = [network.get_input(i) for i in range(network.num_inputs)]
    outputs = [network.get_output(i) for i in range(network.num_outputs)]
    for inp in inputs:
        LOGGER.info(f'{prefix} input "{inp.name}" with shape{inp.shape} {inp.dtype}')
    for out in outputs:
        LOGGER.info(f'{prefix} output "{out.name}" with shape{out.shape} {out.dtype}')

    if dynamic:
        if im.shape[0] <= 1:
            LOGGER.warning(f'{prefix} WARNING ⚠️ --dynamic model requires maximum --batch-size argument')
        profile = builder.create_optimization_profile()
        for inp in inputs:
            profile.set_shape(inp.name, (1, *im.shape[1:]), (max(1, im.shape[0] // 2), *im.shape[1:]), im.shape)
        config.add_optimization_profile(profile)

    LOGGER.info(f'{prefix} building FP{16 if builder.platform_has_fast_fp16 and half else 32} engine as {f}')
    if builder.platform_has_fast_fp16 and half:
        config.set_flag(trt.BuilderFlag.FP16)
    with builder.build_engine(network, config) as engine, open(f, 'wb') as t:
        t.write(engine.serialize())
    return f, None

@try_export
def export_rknn(model, batch_size, int8, data, prefix=colorstr('RKNN:')):
    # YOLOv5 RKNN export
    from rknn.api import RKNN
    # Create RKNN object
    rknn = RKNN(verbose=False)
    rknn.config(mean_values=[[0, 0, 0]], std_values=[
                    [255, 255, 255]], target_platform=os.getenv("RKNN_PLATFORM", "rk3588").lower())
    
    rknn.load_onnx(model=str(model.with_suffix(".onnx")))
    rknn.build(do_quantization=int8, dataset=data, rknn_batch_size=batch_size)
    f = model.with_suffix('.rknn')
    rknn.export_rknn(str(f))
    rknn.release()
    return f, None


@smart_inference_mode()
def run(
        data=ROOT / 'data/coco128.yaml',  # 'dataset.yaml path'
        weights=ROOT / 'yolov5s.pt',  # weights path
        imgsz=(640, 640),  # image (height, width)
        batch_size=1,  # batch size
        device='cpu',  # cuda device, i.e. 0 or 0,1,2,3 or cpu
        include=('torchscript', 'onnx'),  # include formats
        half=False,  # FP16 half-precision export
        inplace=False,  # set YOLOv5 Detect() inplace=True
        optimize=False,  # TorchScript: optimize for mobile
        int8=False,  # CoreML/TF INT8 quantization
        dynamic=False,  # ONNX/TF/TensorRT: dynamic axes
        simplify=False,  # ONNX: simplify model
        opset=12,  # ONNX: opset version
        verbose=False,  # TensorRT: verbose log
        workspace=4,  # TensorRT: workspace size (GB)
):
    t = time.time()
    include = [x.lower() for x in include]  # to lowercase
    fmts = tuple(export_formats()['Argument'])  # --include arguments
    flags = [x in include for x in fmts]
    assert sum(flags) == len(include), f'ERROR: Invalid --include {include}, valid --include arguments are {fmts}'
    onnx, xml, engine, _ = flags  # export booleans
    file = Path(url2file(weights) if str(weights).startswith(('http:/', 'https:/')) else weights)  # PyTorch weights

    # Load PyTorch model
    device = select_device(device)
    if half:
        assert device.type != 'cpu', '--half only compatible with GPU export, i.e. use --device 0'
        assert not dynamic, '--half not compatible with --dynamic, i.e. use either --half or --dynamic but not both'
    model = attempt_load(weights, device=device, inplace=True, fuse=True)  # load FP32 model

    # Checks
    imgsz *= 2 if len(imgsz) == 1 else 1  # expand
    if optimize:
        assert device.type == 'cpu', '--optimize not compatible with cuda devices, i.e. use --device cpu'

    # Input
    gs = int(max(model.stride))  # grid size (max stride)
    imgsz = [check_img_size(x, gs) for x in imgsz]  # verify img_size are gs-multiples
    if rknpu:
        if batch_size != 1: LOGGER.info(f'Ignoring batch size in ONNX export for RKNN export')
        im = torch.zeros(1, 3, *imgsz).to(device)  # image size(1,3,320,192) BCHW iDetection
    else:
        im = torch.zeros(batch_size, 3, *imgsz).to(device)  # image size(1,3,320,192) BCHW iDetection

    # Update model
    model.eval()
    for k, m in model.named_modules():
        if isinstance(m, Detect):
            m.inplace = inplace
            m.dynamic = dynamic
            m.export = True

        if rknpu:
            from models.common import Focus
            from models.common import Conv
            from models.common_rk_plug_in import surrogate_focus
            if isinstance(model.model[0], Focus):
                # For yolo v5 version
                surrogate_focous = surrogate_focus(int(model.model[0].conv.conv.weight.shape[1]/4),
                                                model.model[0].conv.conv.weight.shape[0],
                                                k=tuple(model.model[0].conv.conv.weight.shape[2:4]),
                                                s=model.model[0].conv.conv.stride,
                                                p=model.model[0].conv.conv.padding,
                                                g=model.model[0].conv.conv.groups,
                                                act=True)
                surrogate_focous.conv.conv.weight = model.model[0].conv.conv.weight
                surrogate_focous.conv.conv.bias = model.model[0].conv.conv.bias
                surrogate_focous.conv.act = model.model[0].conv.act
                temp_i = model.model[0].i
                temp_f = model.model[0].f

                model.model[0] = surrogate_focous
                model.model[0].i = temp_i
                model.model[0].f = temp_f
                model.model[0].eval()
            elif isinstance(model.model[0], Conv) and model.model[0].conv.kernel_size == (6, 6):
                # For yolo v6 version
                surrogate_focous = surrogate_focus(model.model[0].conv.weight.shape[1],
                                                model.model[0].conv.weight.shape[0],
                                                k=(3,3), # 6/2, 6/2
                                                s=1,
                                                p=(1,1), # 2/2, 2/2
                                                g=model.model[0].conv.groups,
                                                act=hasattr(model.model[0], 'act'))
                surrogate_focous.conv.conv.weight[:,:3,:,:] = model.model[0].conv.weight[:,:,::2,::2]
                surrogate_focous.conv.conv.weight[:,3:6,:,:] = model.model[0].conv.weight[:,:,1::2,::2]
                surrogate_focous.conv.conv.weight[:,6:9,:,:] = model.model[0].conv.weight[:,:,::2,1::2]
                surrogate_focous.conv.conv.weight[:,9:,:,:] = model.model[0].conv.weight[:,:,1::2,1::2]
                surrogate_focous.conv.conv.bias = model.model[0].conv.bias
                surrogate_focous.conv.act = model.model[0].act
                temp_i = model.model[0].i
                temp_f = model.model[0].f

                model.model[0] = surrogate_focous
                model.model[0].i = temp_i
                model.model[0].f = temp_f
                model.model[0].eval()

    if rknpu:
        if isinstance(model.model[-1], Detect):
            # save anchors
            print('---> save anchors for RKNN')
            RK_anchors = model.model[-1].stride.reshape(3,1).repeat(1,3).reshape(-1,1)* model.model[-1].anchors.reshape(9,2)
            with open('RK_anchors.txt', 'w') as anf:
                # anf.write(str(model.model[-1].na)+'\n')
                for _v in RK_anchors.numpy().flatten():
                    anf.write(str(_v)+'\n')
            RK_anchors = RK_anchors.tolist()
            print(RK_anchors)

        if isinstance(model.model[-1], Segment):
            print("export segment model for RKNPU")
            model.model[-1]._register_seg_seperate(True)
        else:
            print("export detect model for RKNPU")
            model.model[-1]._register_detect_seperate(True)

    for _ in range(2):
        y = model(im)  # dry runs
    if half:
        im, model = im.half(), model.half()  # to FP16
    shape = tuple((y[0] if (isinstance(y, tuple) or (isinstance(y, list))) else y).shape)  # model output shape
    metadata = {'stride': int(max(model.stride)), 'names': model.names}  # model metadata
    LOGGER.info(f"\n{colorstr('PyTorch:')} starting from {file} with output shape {shape} ({file_size(file):.1f} MB)")

    # Exports
    f = [''] * len(fmts)  # exported filenames
    warnings.filterwarnings(action='ignore', category=torch.jit.TracerWarning)  # suppress TracerWarning
    if engine:  # TensorRT required before ONNX
        f[0], _ = export_engine(model, im, file, half, dynamic, simplify, workspace, verbose)
    if onnx or xml or rknpu:  # OpenVINO and RKNN requires ONNX
        f[1], _ = export_onnx(model, im, file, opset, dynamic, simplify)
    if xml:  # OpenVINO
        f[2], _ = export_openvino(file, metadata, half)
    if rknpu:
        f[3], _ = export_rknn(file, batch_size, int8, data)

    # Finish
    f = [str(x) for x in f if x]  # filter out '' and None
    if any(f):
        cls, det, seg = (isinstance(model, x) for x in (ClassificationModel, DetectionModel, SegmentationModel))  # type
        det &= not seg  # segmentation models inherit from SegmentationModel(DetectionModel)
        dir = Path('segment' if seg else 'classify' if cls else '')
        h = '--half' if half else ''  # --half FP16 inference arg
        s = '# WARNING ⚠️ ClassificationModel not yet supported for PyTorch Hub AutoShape inference' if cls else \
            '# WARNING ⚠️ SegmentationModel not yet supported for PyTorch Hub AutoShape inference' if seg else ''
        LOGGER.info(f'\nExport complete ({time.time() - t:.1f}s)'
                    f"\nResults saved to {colorstr('bold', file.parent.resolve())}"
                    f"\nDetect:          python {dir / ('detect.py' if det else 'predict.py')} --weights {f[-1]} {h}"
                    f"\nValidate:        python {dir / 'val.py'} --weights {f[-1]} {h}"
                    f"\nPyTorch Hub:     model = torch.hub.load('ultralytics/yolov5', 'custom', '{f[-1]}')  {s}"
                    f'\nVisualize:       https://netron.app')
    return f  # return list of exported files/dirs


def parse_opt(known=False):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=str, default=ROOT / 'data/coco128.yaml', help='dataset.yaml path')
    parser.add_argument('--weights', nargs='+', type=str, default=ROOT / 'yolov5s.pt', help='model.pt path(s)')
    parser.add_argument('--imgsz', '--img', '--img-size', nargs='+', type=int, default=[640, 640], help='image (h, w)')
    parser.add_argument('--batch-size', type=int, default=1, help='batch size')
    parser.add_argument('--device', default='cpu', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--half', action='store_true', help='FP16 half-precision export')
    parser.add_argument('--inplace', action='store_true', help='set YOLOv5 Detect() inplace=True')
    parser.add_argument('--optimize', action='store_true', help='TorchScript: optimize for mobile')
    parser.add_argument('--int8', action='store_true', help='CoreML/TF INT8 quantization')
    parser.add_argument('--dynamic', action='store_true', help='ONNX/TF/TensorRT: dynamic axes')
    parser.add_argument('--simplify', action='store_true', help='ONNX: simplify model')
    parser.add_argument('--opset', type=int, default=17, help='ONNX: opset version')
    parser.add_argument('--verbose', action='store_true', help='TensorRT: verbose log')
    parser.add_argument('--workspace', type=int, default=4, help='TensorRT: workspace size (GB)')
    parser.add_argument('--include',
                        nargs='+',
                        default=['onnx'],
                        help='onnx, openvino, engine')
    parser.add_argument('--rknpu', action='store_true', help='RKNN npu platform')
    opt = parser.parse_known_args()[0] if known else parser.parse_args()
    print_args(vars(opt))
    return opt


def main(opt):
    for opt.weights in (opt.weights if isinstance(opt.weights, list) else [opt.weights]):
        run(**vars(opt))


if __name__ == '__main__':
    opt = parse_opt()
    del opt.rknpu
    main(opt)
