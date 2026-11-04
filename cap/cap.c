/* chalk-cap: grabs small pieces of the screen for chalk's overlay, fast and only when they change.
 *
 * One process stays connected to the compositor (wlr-screencopy), so a grab costs a buffer copy, not a
 * grim process and a PPM. With "damage" on, a grab waits until something in that region is redrawn: while
 * nothing on screen moves, it costs nothing.
 *
 *   chalk-cap serve        reads requests on stdin, one per line:
 *                            <output> <x> <y> <w> <h> <damage 0|1> <timeout ms> [mode]
 *                          (output-local layout px) and answers each with a header line
 *                            <W> <H> <ms>           then W x H px, one per layout px: mode 0 gray smoothed
 *                                                    like follow.blur, 1 RGB triples, 2 plain gray; ms =
 *                                                    CLOCK_MONOTONIC time of the frame
 *                          or "timeout" / "fail".
 *   chalk-cap probe <output> <seconds> <ink rrggbb> <x> <y> <w> <h> [dumpfile]
 *                          the bench's eye: every frame of that region, prints "t_ms tx ty tn ix iy in" =
 *                          centroid (output layout px) and count of the magenta (ff00ff) and ink-coloured
 *                          pixels; with a dumpfile, also writes the frames for a screen recording.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include "wlr-screencopy-unstable-v1-client-protocol.h"

struct out {
	struct wl_output *wl;
	char name[64];
	int32_t w, h;          /* mode, physical px */
	int32_t scale;
	struct out *next;
};

static struct wl_display *dpy;
static struct wl_shm *shm;
static struct zwlr_screencopy_manager_v1 *scm;
static struct out *outs;

static void out_geometry(void *d, struct wl_output *o, int32_t x, int32_t y, int32_t pw, int32_t ph,
                         int32_t sub, const char *make, const char *model, int32_t tr) {}
static void out_mode(void *d, struct wl_output *o, uint32_t flags, int32_t w, int32_t h, int32_t r) {
	struct out *out = d;
	if (flags & WL_OUTPUT_MODE_CURRENT) { out->w = w; out->h = h; }
}
static void out_done(void *d, struct wl_output *o) {}
static void out_scale(void *d, struct wl_output *o, int32_t s) { ((struct out *)d)->scale = s; }
static void out_name(void *d, struct wl_output *o, const char *n) {
	snprintf(((struct out *)d)->name, sizeof ((struct out *)d)->name, "%s", n);
}
static void out_desc(void *d, struct wl_output *o, const char *n) {}
static const struct wl_output_listener out_l = {out_geometry, out_mode, out_done, out_scale, out_name, out_desc};

static void reg_global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t ver) {
	if (!strcmp(iface, wl_shm_interface.name)) {
		shm = wl_registry_bind(r, name, &wl_shm_interface, 1);
	} else if (!strcmp(iface, zwlr_screencopy_manager_v1_interface.name)) {
		scm = wl_registry_bind(r, name, &zwlr_screencopy_manager_v1_interface, ver < 3 ? ver : 3);
	} else if (!strcmp(iface, wl_output_interface.name) && ver >= 4) {
		struct out *o = calloc(1, sizeof *o);
		o->scale = 1;
		o->wl = wl_registry_bind(r, name, &wl_output_interface, 4);
		wl_output_add_listener(o->wl, &out_l, o);
		o->next = outs;
		outs = o;
	}
}
static void reg_remove(void *d, struct wl_registry *r, uint32_t name) {}
static const struct wl_registry_listener reg_l = {reg_global, reg_remove};

/* ---------- one frame ---------- */

struct frame {
	uint32_t format, w, h, stride;
	int have_buffer, done, failed, ready, yinv;
	uint64_t ms;
};

static void fr_buffer(void *d, struct zwlr_screencopy_frame_v1 *f, uint32_t fmt, uint32_t w, uint32_t h, uint32_t stride) {
	struct frame *fr = d;
	fr->format = fmt; fr->w = w; fr->h = h; fr->stride = stride; fr->have_buffer = 1;
}
static void fr_flags(void *d, struct zwlr_screencopy_frame_v1 *f, uint32_t flags) {
	((struct frame *)d)->yinv = flags & ZWLR_SCREENCOPY_FRAME_V1_FLAGS_Y_INVERT;
}
static void fr_ready(void *d, struct zwlr_screencopy_frame_v1 *f, uint32_t hi, uint32_t lo, uint32_t ns) {
	struct frame *fr = d;
	fr->ms = (((uint64_t)hi << 32 | lo) * 1000ull) + ns / 1000000ull;
	fr->ready = 1;
}
static void fr_failed(void *d, struct zwlr_screencopy_frame_v1 *f) { ((struct frame *)d)->failed = 1; }
static void fr_damage(void *d, struct zwlr_screencopy_frame_v1 *f, uint32_t x, uint32_t y, uint32_t w, uint32_t h) {}
static void fr_dmabuf(void *d, struct zwlr_screencopy_frame_v1 *f, uint32_t fmt, uint32_t w, uint32_t h) {}
static void fr_buffer_done(void *d, struct zwlr_screencopy_frame_v1 *f) { ((struct frame *)d)->done = 1; }
static const struct zwlr_screencopy_frame_v1_listener fr_l = {fr_buffer, fr_flags, fr_ready, fr_failed,
                                                             fr_damage, fr_dmabuf, fr_buffer_done};

/* a reusable shm buffer, regrown when a bigger grab comes along */
static struct { int fd; void *data; size_t size; struct wl_shm_pool *pool; } mem = {-1};

static struct wl_buffer *buffer_for(struct frame *fr) {
	size_t need = (size_t)fr->stride * fr->h;
	if (need > mem.size) {
		if (mem.pool) { wl_shm_pool_destroy(mem.pool); munmap(mem.data, mem.size); close(mem.fd); }
		mem.size = need + need / 4;
		mem.fd = memfd_create("chalk-cap", MFD_CLOEXEC);
		if (mem.fd < 0 || ftruncate(mem.fd, mem.size) < 0) return NULL;
		mem.data = mmap(NULL, mem.size, PROT_READ | PROT_WRITE, MAP_SHARED, mem.fd, 0);
		if (mem.data == MAP_FAILED) return NULL;
		mem.pool = wl_shm_create_pool(shm, mem.fd, mem.size);
	}
	return wl_shm_pool_create_buffer(mem.pool, 0, fr->w, fr->h, fr->stride, fr->format);
}

static uint64_t now_ms(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000ull + ts.tv_nsec / 1000000;
}

/* Dispatch until `flag` is set, the frame fails, or `deadline` (ms, 0 = none) passes. 1 = flag set. */
static int wait_for(struct frame *fr, int *flag, uint64_t deadline) {
	while (!*flag && !fr->failed) {
		while (wl_display_prepare_read(dpy) != 0) wl_display_dispatch_pending(dpy);
		wl_display_flush(dpy);
		int left = -1;
		if (deadline) {
			uint64_t n = now_ms();
			left = n >= deadline ? 0 : (int)(deadline - n);
		}
		struct pollfd p = {wl_display_get_fd(dpy), POLLIN};
		int r = poll(&p, 1, left);
		if (r <= 0) { wl_display_cancel_read(dpy); if (r == 0) return 0; if (errno == EINTR) continue; return -1; }
		if (wl_display_read_events(dpy) < 0) return -1;
		wl_display_dispatch_pending(dpy);
	}
	return *flag ? 1 : -1;
}

/* Grab a region of an output into mem.data. 1 = ok, 0 = timed out waiting for damage, -1 = failed. */
static int grab(struct out *o, int x, int y, int w, int h, int damage, int timeout_ms, struct frame *fr,
                struct wl_buffer **buf) {
	memset(fr, 0, sizeof *fr);
	struct zwlr_screencopy_frame_v1 *f = w > 0
		? zwlr_screencopy_manager_v1_capture_output_region(scm, 0, o->wl, x, y, w, h)
		: zwlr_screencopy_manager_v1_capture_output(scm, 0, o->wl);
	zwlr_screencopy_frame_v1_add_listener(f, &fr_l, fr);
	uint64_t deadline = timeout_ms > 0 ? now_ms() + timeout_ms : 0;
	if (wait_for(fr, zwlr_screencopy_manager_v1_get_version(scm) >= 3 ? &fr->done : &fr->have_buffer, 0) != 1) {
		zwlr_screencopy_frame_v1_destroy(f);
		return -1;
	}
	*buf = buffer_for(fr);
	if (!*buf) { zwlr_screencopy_frame_v1_destroy(f); return -1; }
	if (damage) zwlr_screencopy_frame_v1_copy_with_damage(f, *buf);
	else zwlr_screencopy_frame_v1_copy(f, *buf);
	int r = wait_for(fr, &fr->ready, deadline);
	zwlr_screencopy_frame_v1_destroy(f);
	if (r != 1) { wl_buffer_destroy(*buf); *buf = NULL; }
	return r;
}

static struct out *find_out(const char *name) {
	for (struct out *o = outs; o; o = o->next)
		if (!strcmp(o->name, name) || !strcmp(name, "-")) return o;
	return NULL;
}

/* green channel of pixel (px, py) of the grabbed frame (XRGB/XBGR/ARGB/ABGR 8888 all keep G in byte 1) */
static inline const uint8_t *pixel(struct frame *fr, uint32_t px, uint32_t py) {
	if (fr->yinv) py = fr->h - 1 - py;
	return (const uint8_t *)mem.data + (size_t)py * fr->stride + px * 4;
}

/* 3x3 [1 2 1] smoothing in place (the 1 px border stays), so text compares well at fractional scroll
 * positions; follow.py's blur() does the same to pictures that don't come from here. */
static void blur(uint8_t *g, int w, int h) {
	static uint16_t *row;
	static size_t row_size;
	if (w < 3 || h < 3) return;
	if ((size_t)w * h > row_size) { free(row); row_size = (size_t)w * h; row = malloc(row_size * 2); }
	for (int j = 1; j < h - 1; j++)
		for (int i = 0; i < w; i++)
			row[(size_t)j * w + i] = g[(size_t)(j - 1) * w + i] + 2 * g[(size_t)j * w + i] + g[(size_t)(j + 1) * w + i];
	for (int j = 1; j < h - 1; j++) {
		const uint16_t *r = row + (size_t)j * w;
		for (int i = 1; i < w - 1; i++)
			g[(size_t)j * w + i] = (r[i - 1] + 2 * r[i] + r[i + 1] + 8) / 16;
	}
}

static int serve(void) {
	char line[256], name[64];
	static uint8_t *out;
	static size_t out_size;
	setvbuf(stdout, NULL, _IOFBF, 1 << 16);
	while (fgets(line, sizeof line, stdin)) {
		int x, y, w, h, damage, timeout, mode = 0;
		if (sscanf(line, "%63s %d %d %d %d %d %d %d", name, &x, &y, &w, &h, &damage, &timeout, &mode) < 7 || w <= 0 || h <= 0) {
			puts("fail"); fflush(stdout); continue;
		}
		struct out *o = find_out(name);
		struct frame fr;
		struct wl_buffer *buf = NULL;
		int r = o ? grab(o, x, y, w, h, damage, timeout, &fr, &buf) : -1;
		if (r != 1) { puts(r == 0 ? "timeout" : "fail"); fflush(stdout); continue; }
		/* one gray byte per layout px: the mean of the physical px it covers (the scale can be fractional,
		 * and plain sampling would make text shimmer as it scrolls by fractions of a layout px) */
		int rgb = mode == 1, ch = rgb ? 3 : 1, bgr = fr.format == WL_SHM_FORMAT_XBGR8888 || fr.format == WL_SHM_FORMAT_ABGR8888;
		size_t n = (size_t)w * h * ch;
		if (n > out_size) { free(out); out = malloc(n); out_size = n; }
		for (int j = 0; j < h; j++) {
			uint32_t y0 = (uint32_t)((double)j * fr.h / h), y1 = (uint32_t)((double)(j + 1) * fr.h / h);
			if (y1 <= y0) y1 = y0 + 1;
			if (y1 > fr.h) y1 = fr.h;
			for (int i = 0; i < w; i++) {
				uint32_t x0 = (uint32_t)((double)i * fr.w / w), x1 = (uint32_t)((double)(i + 1) * fr.w / w);
				if (x1 <= x0) x1 = x0 + 1;
				if (x1 > fr.w) x1 = fr.w;
				uint32_t s[3] = {0}, c = 0;
				for (uint32_t py = y0; py < y1; py++)
					for (uint32_t px = x0; px < x1; px++) {
						const uint8_t *p = pixel(&fr, px, py);
						s[1] += p[1];
						if (rgb) { s[0] += p[bgr ? 0 : 2]; s[2] += p[bgr ? 2 : 0]; }
						c++;
					}
				uint8_t *d = out + ((size_t)j * w + i) * ch;
				if (rgb) { d[0] = s[0] / c; d[1] = s[1] / c; d[2] = s[2] / c; }
				else d[0] = s[1] / c;
			}
		}
		wl_buffer_destroy(buf);
		if (mode == 0) blur(out, w, h);
		printf("%d %d %llu\n", w, h, (unsigned long long)fr.ms);
		fwrite(out, 1, n, stdout);
		fflush(stdout);
	}
	return 0;
}

/* ---------- the bench's eye ---------- */

/* Every frame of a region (output layout px): the centroids of the ground truth and the ink, and with a
 * dump file, the region itself at half its physical size (RGB, after an 8-byte ms stamp; a "W H\n" header). */
static int probe(const char *name, double seconds, uint32_t ink, int x, int y, int w, int h, FILE *dump) {
	struct out *o = find_out(name);
	if (!o) { fprintf(stderr, "chalk-cap: no output %s\n", name); return 1; }
	uint8_t ir = ink >> 16, ig = ink >> 8, ib = ink;
	uint8_t *rgb = NULL;
	uint64_t end = now_ms() + (uint64_t)(seconds * 1000);
	setvbuf(stdout, NULL, _IOLBF, 0);
	while (now_ms() < end) {
		struct frame fr;
		struct wl_buffer *buf = NULL;
		int r = grab(o, x, y, w, h, 1, (int)(end - now_ms()) + 1, &fr, &buf);
		if (r == 0) break;
		if (r < 0) return 1;
		double k = (double)w / fr.w;  /* buffer px -> layout px */
		double tx = 0, ty = 0, ix = 0, iy = 0;
		long tn = 0, in = 0;
		int bgr = fr.format == WL_SHM_FORMAT_XBGR8888 || fr.format == WL_SHM_FORMAT_ABGR8888;
		for (uint32_t py = 0; py < fr.h; py++) {
			for (uint32_t px = 0; px < fr.w; px++) {
				const uint8_t *p = pixel(&fr, px, py);
				int R = bgr ? p[0] : p[2], G = p[1], B = bgr ? p[2] : p[0];
				if (R > 220 && G < 40 && B > 220) { tx += px; ty += py; tn++; }
				else if (abs(R - ir) < 30 && abs(G - ig) < 30 && abs(B - ib) < 30) { ix += px; iy += py; in++; }
			}
		}
		if (dump) {
			uint32_t dw = fr.w / 2, dh = fr.h / 2;
			if (!rgb) { rgb = malloc((size_t)dw * dh * 3); fprintf(dump, "%u %u\n", dw, dh); }
			for (uint32_t j = 0; j < dh; j++)
				for (uint32_t i = 0; i < dw; i++) {
					const uint8_t *p = pixel(&fr, 2 * i, 2 * j);
					uint8_t *d = rgb + ((size_t)j * dw + i) * 3;
					d[0] = bgr ? p[0] : p[2]; d[1] = p[1]; d[2] = bgr ? p[2] : p[0];
				}
			uint64_t stamp = fr.ms;
			fwrite(&stamp, 8, 1, dump);
			fwrite(rgb, 1, (size_t)dw * dh * 3, dump);
		}
		wl_buffer_destroy(buf);
		printf("%llu %.2f %.2f %ld %.2f %.2f %ld\n", (unsigned long long)fr.ms,
		       tn ? x + tx / tn * k : -1, tn ? y + ty / tn * k : -1, tn, in ? x + ix / in * k : -1, in ? y + iy / in * k : -1, in);
	}
	return 0;
}

int main(int argc, char **argv) {
	if (argc < 2) { fprintf(stderr, "usage: chalk-cap serve | probe <output> <seconds> <ink> <x> <y> <w> <h> [dumpfile]\n"); return 2; }
	dpy = wl_display_connect(NULL);
	if (!dpy) { fprintf(stderr, "chalk-cap: no wayland display\n"); return 1; }
	struct wl_registry *reg = wl_display_get_registry(dpy);
	wl_registry_add_listener(reg, &reg_l, NULL);
	wl_display_roundtrip(dpy);
	wl_display_roundtrip(dpy);
	if (!shm || !scm || !outs) { fprintf(stderr, "chalk-cap: compositor lacks wlr-screencopy\n"); return 1; }
	if (!strcmp(argv[1], "serve")) return serve();
	if (!strcmp(argv[1], "probe") && argc >= 9) {
		FILE *dump = argc >= 10 ? fopen(argv[9], "wb") : NULL;
		int r = probe(argv[2], atof(argv[3]), (uint32_t)strtoul(argv[4], NULL, 16), atoi(argv[5]), atoi(argv[6]),
		              atoi(argv[7]), atoi(argv[8]), dump);
		if (dump) fclose(dump);
		return r;
	}
	fprintf(stderr, "chalk-cap: unknown mode %s\n", argv[1]);
	return 2;
}
