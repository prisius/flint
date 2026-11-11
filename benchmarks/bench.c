#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include <stdint.h>
#include <time.h>
static int64_t sieve(int64_t n) {
    bool* c = calloc(n + 1, 1); int64_t count = 0;
    for (int64_t i = 2; i <= n; i++) if (!c[i]) { count++; for (int64_t j = i * i; j <= n; j += i) c[j] = true; }
    free(c); return count;
}
static int fib(int n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }
static double floats(int64_t n) { double x = 0, v = 1; for (int64_t i = 0; i < n; i++) { v -= x * 0.001; x += v * 0.001; } return x; }
int main(void) {
    struct timespec a, b; clock_gettime(CLOCK_MONOTONIC, &a);
    int64_t p = sieve(50000000); int f = fib(38); double x = floats(300000000);
    clock_gettime(CLOCK_MONOTONIC, &b);
    printf("primes=%lld fib=%d x=%.6f\n", (long long)p, f, x);
    fprintf(stderr, "c: %lld ms\n", (long long)((b.tv_sec - a.tv_sec) * 1000 + (b.tv_nsec - a.tv_nsec) / 1000000));
}
