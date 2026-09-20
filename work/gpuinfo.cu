#include <cstdio>
#include <cuda_runtime.h>

#define CHECK(call) do { \
    cudaError_t err = (call); \
    if (err != cudaSuccess) { \
        fprintf(stderr, "CUDA error at %s:%d: %s\n", \
                __FILE__, __LINE__, cudaGetErrorString(err)); \
        return 1; \
    } \
} while(0)

int main() {
    int count = 0;
    CHECK(cudaGetDeviceCount(&count));

    if (count == 0) {
        printf("No CUDA-capable devices found.\n");
        return 1;
    }

    printf("Found %d CUDA device(s):\n\n", count);

    for (int i = 0; i < count; i++) {
        cudaDeviceProp prop;
        CHECK(cudaGetDeviceProperties(&prop, i));

        printf("=== Device %d: %s ===\n", i, prop.name);
        printf("  Compute Capability : %d.%d\n", prop.major, prop.minor);
        printf("  Total Global Mem   : %.2f GB\n", prop.totalGlobalMem / 1073741824.0);
        printf("  Shared Mem/Block   : %zu KB\n", prop.sharedMemPerBlock / 1024);
        printf("  Shared Mem/SM      : %zu KB\n", prop.sharedMemPerMultiprocessor / 1024);
        printf("  L2 Cache           : %d KB\n", prop.l2CacheSize / 1024);
        printf("  SMs                : %d\n", prop.multiProcessorCount);
        printf("  Threads/Block      : %d\n", prop.maxThreadsPerBlock);
        printf("  Max Threads/SM     : %d\n", prop.maxThreadsPerMultiProcessor);
        printf("  Clock Rate         : %d MHz\n", prop.clockRate / 1000);
        printf("  Memory Clock       : %d MHz\n", prop.memoryClockRate / 1000);
        printf("  Mem Bus Width      : %d-bit\n", prop.memoryBusWidth);
        printf("  Mem Bus BW         : %.2f GB/s\n",
               (double)prop.memoryClockRate * 2.0 * (prop.memoryBusWidth / 8) / 1e6);
        printf("  Warp Size          : %d\n", prop.warpSize);
        printf("  Registers/SM       : %d\n", prop.regsPerMultiprocessor);
        printf("  Registers/Block    : %d\n", prop.regsPerBlock);

        int smem_optin = 0;
        cudaDeviceGetAttribute(&smem_optin, cudaDevAttrMaxSharedMemoryPerBlockOptin, i);
        printf("  Shared Mem/Block Optin: %d KB\n", smem_optin / 1024);

        int cc = prop.major * 10 + prop.minor;
        printf("  Tensor Cores       : %s\n", cc >= 75 ? "yes" : "no");

        printf("\n");
    }

    return 0;
}
