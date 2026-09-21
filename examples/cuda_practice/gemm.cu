// Quick FP16 GEMM benchmark using cuBLAS.
// Compile: nvcc -O3 -o gemm gemm.cu -lcublas
// Usage:   ./gemm [N]   (matrix size, default 4096)

#include <cstdio>
#include <cstdlib>
#include <cuda_fp16.h>
#include <cublas_v2.h>

#define CHECK_CUDA(call) do { \
    cudaError_t err = (call); \
    if (err != cudaSuccess) { \
        fprintf(stderr, "CUDA error at %s:%d: %s\n", __FILE__, __LINE__, cudaGetErrorString(err)); \
        exit(1); \
    } \
} while(0)

#define CHECK_CUBLAS(call) do { \
    cublasStatus_t err = (call); \
    if (err != CUBLAS_STATUS_SUCCESS) { \
        fprintf(stderr, "cuBLAS error at %s:%d\n", __FILE__, __LINE__); \
        exit(1); \
    } \
} while(0)

int main(int argc, char** argv) {
    int N = (argc > 1) ? atoi(argv[1]) : 4096;

    printf("FP16 GEMM Benchmark\n");
    printf("Matrix size: %d x %d\n\n", N, N);

    // Allocate device memory
    size_t bytes = (size_t)N * N * sizeof(__half);
    __half *dA, *dB, *dC;
    CHECK_CUDA(cudaMalloc(&dA, bytes));
    CHECK_CUDA(cudaMalloc(&dB, bytes));
    CHECK_CUDA(cudaMalloc(&dC, bytes));

    // Fill with small random values on host (as __half), copy to device
    size_t hn = (size_t)N * N;
    __half *hA = (__half*)malloc(hn * sizeof(__half));
    for (size_t i = 0; i < hn; i++)
        hA[i] = __float2half((float)(rand() % 1000) / 1000.0f - 0.5f);
    CHECK_CUDA(cudaMemcpy(dA, hA, hn * sizeof(__half), cudaMemcpyHostToDevice));
    CHECK_CUDA(cudaMemcpy(dB, hA, hn * sizeof(__half), cudaMemcpyHostToDevice));
    free(hA);

    // cuBLAS handle
    cublasHandle_t handle;
    CHECK_CUBLAS(cublasCreate(&handle));

    float alpha = 1.0f, beta = 0.0f;

    // Warmup (3 iterations)
    for (int i = 0; i < 3; i++) {
        CHECK_CUBLAS(cublasGemmEx(handle,
            CUBLAS_OP_N, CUBLAS_OP_N,
            N, N, N,
            &alpha,
            dA, CUDA_R_16F, N,
            dB, CUDA_R_16F, N,
            &beta,
            dC, CUDA_R_16F, N,
            CUBLAS_COMPUTE_32F,  // FP32 accumulation
            CUBLAS_GEMM_DEFAULT));
    }
    cudaDeviceSynchronize();

    // Timed run (10 iterations)
    const int iters = 10;
    cudaEvent_t start, stop;
    cudaEventCreate(&start);
    cudaEventCreate(&stop);

    cudaEventRecord(start);
    for (int i = 0; i < iters; i++) {
        CHECK_CUBLAS(cublasGemmEx(handle,
            CUBLAS_OP_N, CUBLAS_OP_N,
            N, N, N,
            &alpha,
            dA, CUDA_R_16F, N,
            dB, CUDA_R_16F, N,
            &beta,
            dC, CUDA_R_16F, N,
            CUBLAS_COMPUTE_32F,
            CUBLAS_GEMM_DEFAULT));
    }
    cudaEventRecord(stop);
    cudaEventSynchronize(stop);

    float ms;
    cudaEventElapsedTime(&ms, start, stop);
    float total_ms = ms / iters;

    double flops = 2.0 * (double)N * N * N;  // multiply-add
    double tflops = flops / (total_ms / 1000.0) / 1e12;

    printf("Time per GEMM  : %.3f ms\n", total_ms);
    printf("FLOPs          : %.2f GFLOP\n", flops / 1e9);
    printf("FP16 TFLOPs    : %.2f\n", tflops);

    // Cleanup
    cudaEventDestroy(start);
    cudaEventDestroy(stop);
    cudaFree(dA);
    cudaFree(dB);
    cudaFree(dC);
    cublasDestroy(handle);

    return 0;
}
