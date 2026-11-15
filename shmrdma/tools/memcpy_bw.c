// memcpy bandwidth: chunk-sized copies walking two large buffers, so
// the data comes from DRAM as it would for RDMA between two guests.
// usage: memcpy_bw [threads] [chunk_kb] [buf_mb] [secs] [method]
// method: memcpy (default); x86_64: nt (AVX2 streaming stores);
// aarch64: stp (NEON pairs), stnp (non-temporal stores),
// ldnp (non-temporal loads and stores)
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <stdint.h>
#if defined(__x86_64__)
#include <immintrin.h>
#endif
#ifndef MAP_POPULATE
#define MAP_POPULATE 0		/* macOS: the memset below faults it in */
#endif

typedef void (*copy_fn)(void *, const void *, size_t);
static copy_fn g_copy;

static void copy_memcpy(void *dst, const void *src, size_t len)
{
	memcpy(dst, src, len);
}

#if defined(__x86_64__)
/* copy with non-temporal stores; dst 32-aligned, len multiple of 128 */
__attribute__((target("avx2")))
static void copy_nt(void *dst, const void *src, size_t len)
{
	__m256i *d = dst;
	const __m256i *s = src;

	for (size_t i = 0; i < len / 32; i += 4) {
		__m256i a = _mm256_loadu_si256(s + i);
		__m256i b = _mm256_loadu_si256(s + i + 1);
		__m256i c = _mm256_loadu_si256(s + i + 2);
		__m256i e = _mm256_loadu_si256(s + i + 3);

		_mm256_stream_si256(d + i, a);
		_mm256_stream_si256(d + i + 1, b);
		_mm256_stream_si256(d + i + 2, c);
		_mm256_stream_si256(d + i + 3, e);
	}
	_mm_sfence();
}
#endif

#if defined(__aarch64__)
/* 128 bytes per round through q0-q7; len a multiple of 128 */
#define COPY128(name, ld, st)						\
static void name(void *dst, const void *src, size_t len)		\
{									\
	uint8_t *d = dst;						\
	const uint8_t *s = src;						\
									\
	for (; len; len -= 128, d += 128, s += 128)			\
		__asm__ volatile(					\
			ld " q0, q1, [%1]\n\t"				\
			ld " q2, q3, [%1, #32]\n\t"			\
			ld " q4, q5, [%1, #64]\n\t"			\
			ld " q6, q7, [%1, #96]\n\t"			\
			st " q0, q1, [%0]\n\t"				\
			st " q2, q3, [%0, #32]\n\t"			\
			st " q4, q5, [%0, #64]\n\t"			\
			st " q6, q7, [%0, #96]\n\t"			\
			: : "r"(d), "r"(s)				\
			: "v0", "v1", "v2", "v3", "v4", "v5", "v6",	\
			  "v7", "memory");				\
}
COPY128(copy_stp, "ldp", "stp")
COPY128(copy_stnp, "ldp", "stnp")
COPY128(copy_ldnp, "ldnp", "stnp")
#endif

static const struct {
	const char *name;
	copy_fn fn;
} methods[] = {
	{ "memcpy", copy_memcpy },
#if defined(__x86_64__)
	{ "nt", copy_nt },
#endif
#if defined(__aarch64__)
	{ "stp", copy_stp },
	{ "stnp", copy_stnp },
	{ "ldnp", copy_ldnp },
#endif
};

static size_t chunk, bufsz;
static double secs;
static volatile int stop, go, ready;

static double now(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void *run(void *arg)
{
	char *src = mmap(NULL, bufsz, PROT_READ | PROT_WRITE,
			 MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
	char *dst = mmap(NULL, bufsz, PROT_READ | PROT_WRITE,
			 MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
	size_t off = 0;
	unsigned long long bytes = 0;

	memset(src, 1, bufsz);
	memset(dst, 2, bufsz);
	__atomic_add_fetch(&ready, 1, __ATOMIC_SEQ_CST);
	while (!go)
		;
	while (!stop) {
		g_copy(dst + off, src + off, chunk);
		bytes += chunk;
		off += chunk;
		if (off + chunk > bufsz)
			off = 0;
	}
	*(unsigned long long *)arg = bytes;
	return NULL;
}

int main(int argc, char **argv)
{
	int n = argc > 1 ? atoi(argv[1]) : 1;
	chunk = (argc > 2 ? atol(argv[2]) : 1024) * 1024;
	bufsz = (argc > 3 ? atol(argv[3]) : 1024) * 1024 * 1024;
	secs = argc > 4 ? atof(argv[4]) : 5;
	const char *m = argc > 5 ? argv[5] : "memcpy";

	for (size_t i = 0; i < sizeof(methods) / sizeof(methods[0]); i++)
		if (!strcmp(m, methods[i].name))
			g_copy = methods[i].fn;
	if (!g_copy) {
		fprintf(stderr, "unknown method %s\n", m);
		return 2;
	}
	pthread_t t[256];
	unsigned long long b[256] = {0}, tot = 0;

	for (int i = 0; i < n; i++)
		pthread_create(&t[i], NULL, run, &b[i]);
	struct timespec ts = { 0 };
	while (ready < n)
		;
	go = 1;
	double t0 = now();
	ts.tv_sec = (time_t)secs;
	ts.tv_nsec = (long)((secs - (time_t)secs) * 1e9);
	nanosleep(&ts, NULL);
	stop = 1;
	for (int i = 0; i < n; i++) {
		pthread_join(t[i], NULL);
		tot += b[i];
	}
	double dt = now() - t0;
	printf("threads %d chunk %zuKB %s: %.1f MiB/s\n",
	       n, chunk / 1024, m, tot / dt / 1048576);
	return 0;
}
