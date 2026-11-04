// A virtual pointer for tests in the headless Sway (it has no input devices, so clients never get a pointer).
// Reads commands on stdin, one per line, and keeps the device alive between them:
//   move X Y      (layout px)       click [BUTTON]  (272 = left)       wait MS
//   down [BUTTON] / up [BUTTON]     hold and let go (drag = down, moves, up)
//   wheel N       N mouse wheel clicks (positive = down)
//   swipe DY MS   one touchpad scroll gesture of DY px spread over MS (120 Hz, ease-in-out), then fingers up
//                 (apps start kinetic scrolling); the scroll bench's hand (bench/scroll)
// Build: bench/vpointer/build.sh
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

static struct zwlr_virtual_pointer_manager_v1 *mgr;
static struct wl_seat *seat;

static void global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t v) {
  if (!strcmp(iface, zwlr_virtual_pointer_manager_v1_interface.name)) mgr = wl_registry_bind(r, name, &zwlr_virtual_pointer_manager_v1_interface, 1);
  else if (!strcmp(iface, wl_seat_interface.name) && !seat) seat = wl_registry_bind(r, name, &wl_seat_interface, 1);
}
static void global_remove(void *d, struct wl_registry *r, uint32_t name) {}
static const struct wl_registry_listener reg = { global, global_remove };

static uint32_t ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1000 + t.tv_nsec / 1000000; }

static void axis(struct zwlr_virtual_pointer_v1 *p, enum wl_pointer_axis_source src, double v, int discrete) {
  zwlr_virtual_pointer_v1_axis_source(p, src);
  if (discrete)
    zwlr_virtual_pointer_v1_axis_discrete(p, ms(), WL_POINTER_AXIS_VERTICAL_SCROLL, wl_fixed_from_double(v), discrete);
  else
    zwlr_virtual_pointer_v1_axis(p, ms(), WL_POINTER_AXIS_VERTICAL_SCROLL, wl_fixed_from_double(v));
  zwlr_virtual_pointer_v1_frame(p);
}

int main(int argc, char **argv) {
  uint32_t W = argc > 2 ? atoi(argv[1]) : 1920, H = argc > 2 ? atoi(argv[2]) : 1280;
  struct wl_display *dpy = wl_display_connect(NULL);
  if (!dpy) { fprintf(stderr, "vpointer: no wayland display\n"); return 1; }
  struct wl_registry *r = wl_display_get_registry(dpy);
  wl_registry_add_listener(r, &reg, NULL);
  wl_display_roundtrip(dpy);
  if (!mgr) { fprintf(stderr, "vpointer: compositor has no virtual pointer\n"); return 1; }
  struct zwlr_virtual_pointer_v1 *p = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(mgr, seat);
  wl_display_roundtrip(dpy);
  char line[256];
  setvbuf(stdout, NULL, _IOLBF, 0);
  while (fgets(line, sizeof line, stdin)) {
    int x, y, b = 272;
    double d;
    if (sscanf(line, "move %d %d", &x, &y) == 2) {
      zwlr_virtual_pointer_v1_motion_absolute(p, ms(), x, y, W, H);
      zwlr_virtual_pointer_v1_frame(p);
    } else if (!strncmp(line, "click", 5)) {
      sscanf(line, "click %d", &b);
      zwlr_virtual_pointer_v1_button(p, ms(), b, WL_POINTER_BUTTON_STATE_PRESSED);
      zwlr_virtual_pointer_v1_frame(p);
      wl_display_roundtrip(dpy);
      usleep(40000);
      zwlr_virtual_pointer_v1_button(p, ms(), b, WL_POINTER_BUTTON_STATE_RELEASED);
      zwlr_virtual_pointer_v1_frame(p);
    } else if (!strncmp(line, "down", 4) || !strncmp(line, "up", 2)) {   /* drags: down, move..., up */
      sscanf(line + (line[0] == 'd' ? 4 : 2), "%d", &b);
      zwlr_virtual_pointer_v1_button(p, ms(), b, line[0] == 'd' ? WL_POINTER_BUTTON_STATE_PRESSED : WL_POINTER_BUTTON_STATE_RELEASED);
      zwlr_virtual_pointer_v1_frame(p);
    } else if (sscanf(line, "wait %d", &x) == 1) {
      usleep(x * 1000);
    } else if (sscanf(line, "wheel %d", &x) == 1) {
      for (int i = 0; i < abs(x); i++) {
        axis(p, WL_POINTER_AXIS_SOURCE_WHEEL, x < 0 ? -15.0 : 15.0, x < 0 ? -1 : 1);
        wl_display_flush(dpy);
        if (i + 1 < abs(x)) usleep(40000);
      }
    } else if (sscanf(line, "swipe %lf %d", &d, &x) == 2) {
      int steps = x * 120 / 1000;
      double done = 0;
      for (int i = 1; i <= steps; i++) {
        double t = (double)i / steps, s = t < 0.5 ? 2 * t * t : 1 - pow(-2 * t + 2, 2) / 2;
        axis(p, WL_POINTER_AXIS_SOURCE_FINGER, d * s - done, 0);
        done = d * s;
        wl_display_flush(dpy);
        usleep(1000000 / 120);
      }
      zwlr_virtual_pointer_v1_axis_source(p, WL_POINTER_AXIS_SOURCE_FINGER);
      zwlr_virtual_pointer_v1_axis_stop(p, ms(), WL_POINTER_AXIS_VERTICAL_SCROLL);
      zwlr_virtual_pointer_v1_frame(p);
    }
    wl_display_roundtrip(dpy);
    printf("ok\n");
  }
  zwlr_virtual_pointer_v1_destroy(p);
  wl_display_roundtrip(dpy);
  return 0;
}
