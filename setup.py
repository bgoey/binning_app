from setuptools import setup, Extension
ext = Extension(
    name="binning._monotonic",
    sources=["binning/monotonic_c.c"],
    extra_compile_args=["-O3", "-march=native"],  # removed -ffast-math
)
setup(name="monotonic_binning_c", version="1.0.0", ext_modules=[ext])
