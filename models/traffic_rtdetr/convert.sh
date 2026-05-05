/usr/src/tensorrt/bin/trtexec \
--onnx=rtdetr-l_2_slim.onnx \
--saveEngine=model_b1_gpu0_fp16.engine \
--minShapes=input:1x3x640x640 \
--optShapes=input:1x3x640x640 \
--maxShapes=input:4x3x640x640 \
--fp16