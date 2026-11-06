import os
import platform

print('Python:', platform.python_version())
print('CUDA_VISIBLE_DEVICES:', os.environ.get('CUDA_VISIBLE_DEVICES'))

try:
    import torch
    print('PyTorch:', torch.__version__)
    print('CUDA available:', torch.cuda.is_available())
    if torch.cuda.is_available():
        print('GPU:', torch.cuda.get_device_name(0))
        props = torch.cuda.get_device_properties(0)
        print('GPU VRAM GB:', round(props.total_memory / (1024**3), 2))
        x = torch.randn((2048, 2048), device='cuda')
        y = x @ x
        torch.cuda.synchronize()
        print('GPU matrix test passed:', tuple(y.shape))
    else:
        print('No CUDA GPU detected.')
except Exception as exc:
    print('GPU test error:', repr(exc))
