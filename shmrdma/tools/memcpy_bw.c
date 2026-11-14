// memcpy bandwidth: chunk-sized copies walking two large buffers, so
// the data comes from DRAM as it would for RDMA between two guests.
// usage: memcpy_bw [threads] [chunk_kb] [buf_mb] [secs]
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <immintrin.h>

static int g_nt;

/* copy with non-temporal stores; dst 32-aligned, len multiple of 128 */
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
		if (g_nt)
			copy_nt(dst + off, src + off, chunk);
		else
			memcpy(dst + off, src + off, chunk);
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
	g_nt = argc > 5 && !strcmp(argv[5], "nt");
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
	       n, chunk / 1024, g_nt ? "nt" : "memcpy", tot / dt / 1048576);
	return 0;
}
