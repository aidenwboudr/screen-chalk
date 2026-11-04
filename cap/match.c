/* libchalkmatch: follow.py's inner loop (Anchor.errors) in C, called through ctypes. Same arithmetic as the
 * numpy version it replaces (which stays as the fallback): for each candidate top-left (y, x), the template's
 * sample points against the frame (bilinear between px), skipping points off the frame or under chalk's ink.
 *
 *   err  = mean |frame - val| over the judged points / contrast   (9 when under min_valid of them can be judged)
 *   frac = share of judged points off by more than tau            (1 likewise)
 */
#include <math.h>
#include <stddef.h>
#include <stdint.h>

void chalk_match(const uint8_t *frame, const uint8_t *fmask, int H, int W, int ox, int oy,
                 const int32_t *py, const int32_t *px, const float *val, int n,
                 const float *ys, int nys, const float *xs, int nxs,
                 float contrast, float tau, float min_valid, float *err, float *frac) {
	for (int a = 0; a < nys; a++) {
		float fy0 = floorf(ys[a]), fy = ys[a] - fy0;
		int Y = (int)fy0 - oy;
		for (int b = 0; b < nxs; b++) {
			float fx0 = floorf(xs[b]), fx = xs[b] - fx0;
			int X = (int)fx0 - ox;
			double sum = 0;
			int judged = 0, bad = 0;
			for (int i = 0; i < n; i++) {
				int r = Y + py[i], c = X + px[i];
				if (r < 0 || r >= H - 1 || c < 0 || c >= W - 1) continue;
				size_t k = (size_t)r * W + c;
				if (!fmask[k]) continue;
				float v = frame[k];
				if (fx != 0.0f)
					v = (v * (1 - fx) + frame[k + 1] * fx) * (1 - fy) + (frame[k + W] * (1 - fx) + frame[k + W + 1] * fx) * fy;
				else if (fy != 0.0f)
					v = v * (1 - fy) + frame[k + W] * fy;
				float d = fabsf(v - val[i]);
				sum += d;
				judged++;
				bad += d > tau;
			}
			int o = a * nxs + b;
			if (judged < min_valid * n) {
				err[o] = 9.0f;
				frac[o] = 1.0f;
			} else {
				err[o] = (float)(sum / judged / contrast);
				frac[o] = (float)bad / judged;
			}
		}
	}
}
