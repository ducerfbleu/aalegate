NVCC      := nvcc
NVCCFLAGS := -O3
CUBLAS    := -lcublas

TARGETS   := gpuinfo gemm

all: $(TARGETS)

gpuinfo: gpuinfo.cu
	$(NVCC) $(NVCCFLAGS) -o $@ $<

gemm: gemm.cu
	$(NVCC) $(NVCCFLAGS) -o $@ $< $(CUBLAS)

clean:
	rm -f $(TARGETS)

.PHONY: all clean
