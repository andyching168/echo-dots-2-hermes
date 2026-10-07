#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include "speex/speex_echo.h"

#define PCM_OUT 0x00000000
#define PCM_IN 0x10000000
struct pcm_config {
  unsigned channels, rate, period_size, period_count;
  int format;
  unsigned start_threshold, stop_threshold, silence_threshold;
  int silence_size;
  unsigned avail_min;
};
struct pcm;
extern struct pcm *pcm_open(unsigned, unsigned, unsigned, const struct pcm_config *);
extern int pcm_close(struct pcm *);
extern int pcm_read(struct pcm *, void *, unsigned);
extern int pcm_write(struct pcm *, const void *, unsigned);
extern int pcm_is_ready(struct pcm *);
extern const char *pcm_get_error(struct pcm *);
extern unsigned pcm_get_buffer_size(const struct pcm *);
extern int pcm_prepare(struct pcm *);

/* Phase 5D.6A: tinyalsa mixer API for the idle speaker-amp gate. */
struct mixer;
struct mixer_ctl;
extern struct mixer *mixer_open(unsigned int card);
extern void mixer_close(struct mixer *mixer);
extern int mixer_get_num_ctls(struct mixer *mixer);
extern struct mixer_ctl *mixer_get_ctl(struct mixer *mixer, unsigned int id);
extern const char *mixer_ctl_get_name(struct mixer_ctl *ctl);
extern int mixer_ctl_set_value(struct mixer_ctl *ctl, unsigned int id, int value);
extern int mixer_ctl_get_value(struct mixer_ctl *ctl, unsigned int id);

#define MAGIC 0xE0D1
#define T_UPLINK 0x01
#define T_DOWNLINK 0x02
#define T_METER 0x03
#define T_FLUSH 0x80
#define T_RESET 0x82
#define T_HELLO 0x84
#define T_HELLO_ACK 0x85

#define CAP_PERIOD 512
#define CAP_COUNT 5
#define CHANS 9
#define AEC_FRAME 160
#define AEC_TAIL (300 * 16000 / 1000)
#define MIC_CH 6
#define REF_CH 7

#define DL_SR 48000
#define DL_CHUNK 480
#define LIN_CAP 8192

static volatile int g_stop = 0;
static int g_mode = 0; /* 0 = net, 1 = local */
static int g_sock = -1;
static struct pcm *g_cap = NULL, *g_pb = NULL;
static pthread_t g_play_th, g_rx_th;
static int g_play_started = 0, g_rx_started = 0;
static SpeexEchoState *g_st = NULL;
static unsigned long long g_frames_sent = 0, g_tx_drops = 0;
static int g_cap_errs = 0;
static double g_sum_in = 0.0, g_sum_out = 0.0;
static int g_mixer_touched = 0;
static char g_server[128];
static int g_port = 0;
static pthread_mutex_t g_tx = PTHREAD_MUTEX_INITIALIZER;

/* ring (net downlink) */
static int16_t *g_ring = NULL;
static int g_ring_cap = 4800;
static int g_ring_head = 0, g_ring_tail = 0, g_ring_count = 0;
static int g_prebuf = 2880, g_high = 4320, g_target = 2880, g_low = 1440;
static pthread_mutex_t g_ring_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_ring_cond = PTHREAD_COND_INITIALIZER;
static volatile unsigned long long g_underruns = 0, g_underrun_frames = 0;
static volatile unsigned long long g_dropped_frames = 0, g_trims = 0, g_trim_frames = 0;
static volatile unsigned long long g_dl_recv = 0, g_dl_gaps = 0, g_dl_dupes = 0;
static volatile uint32_t g_dl_last = 0;
static int g_dl_have_last = 0;
static volatile int g_q_depth = 0, g_q_max = 0;
static volatile int g_ref_rms = 0, g_out_rms = 0;
static volatile int g_alsa_buf_frames = 0;
static volatile unsigned long long g_pb_errs = 0, g_pb_writes = 0, g_pb_ns = 0;
/* Phase 5D.6A instrumentation */
static volatile unsigned long long g_ev_pb_err = 0, g_ev_prepare = 0, g_ev_catchup = 0;
static volatile unsigned long long g_ev_zero_underrun = 0, g_ev_zero_data = 0;
static volatile int g_pb_open_only = 0;
static volatile int g_pb_rms = 0, g_pb_peak = 0;
static volatile unsigned long long g_pb_nonzero = 0, g_pb_samples = 0;
static long g_pb_rms_acc = 0, g_pb_peak_acc = 0;
static unsigned long long g_pb_nz_acc = 0, g_pb_n_acc = 0;
/* Phase 5D.6A idle speaker-amp gate */
static struct mixer *g_mix = NULL;
static struct mixer_ctl *g_amp_ctl = NULL;
static int g_gate = 0, g_gate_ms = 1200, g_settle_ms = 12, g_fade_ms = 8;
static volatile int g_amp_on = 1;
static volatile unsigned long long g_gate_offs = 0, g_gate_ons = 0;
static volatile long g_silent_ms = 0;
/* Phase 5D.6A uplink-throttle test knobs (chirp isolation) */
static int g_no_uplink = 0, g_up_every = 1;
static unsigned long long g_aec_frames = 0;
static volatile double g_step_ppm = 0.0;
static volatile double g_last_step = 1.0;
static volatile unsigned long long g_resample_corrections = 0;

/* local source */
static int16_t *g_local = NULL;
static long g_local_len = 0;
static long g_local_pos = 0;

/* liner / ASRC */
static int16_t g_lin[LIN_CAP];
static int g_lin_head = 0, g_lin_len = 0;
static double g_lin_frac = 0.0;

static int g_drift = 1; /* 0 discrete, 1 resample */
static int g_force_ppm = 0;
static FILE *g_qlog = NULL;
static FILE *g_rlog = NULL;
static FILE *g_refraw = NULL, *g_micraw = NULL, *g_outraw = NULL;
static FILE *g_ref8raw = NULL, *g_pbraw = NULL;
static volatile unsigned long long g_ev_underrun = 0, g_ev_drop = 0, g_ev_trim = 0;

static void on_sig(int s) { (void)s; g_stop = 1; }

static void now_mono_us(uint64_t *out) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  *out = (uint64_t)t.tv_sec * 1000000ULL + (uint64_t)t.tv_nsec / 1000ULL;
}

static double now_s(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec + t.tv_nsec / 1e9;
}

static int getenv_int(const char *k, int d) {
  const char *v = getenv(k);
  return v && *v ? atoi(v) : d;
}

static int read_file_contains(const char *path, const char *needle) {
  FILE *f = fopen(path, "r");
  if (!f) return 0;
  char buf[4096];
  size_t n = fread(buf, 1, sizeof(buf) - 1, f);
  fclose(f);
  buf[n] = 0;
  return strstr(buf, needle) != NULL;
}

static int getprop_equals(const char *key, const char *want) {
  char cmd[256], out[256];
  snprintf(cmd, sizeof cmd, "getprop %s", key);
  FILE *p = popen(cmd, "r");
  if (!p) return 0;
  if (!fgets(out, sizeof out, p)) { pclose(p); return 0; }
  pclose(p);
  out[strcspn(out, "\r\n")] = 0;
  return strcmp(out, want) == 0;
}

static int d24_owner_is_mixer_only(void) {
  const char *cmd =
      "for p in $(ls /proc | grep -E '^[0-9]+$'); do "
      "ls -l /proc/$p/fd 2>/dev/null | grep -q pcmC0D24c && "
      "cat /proc/$p/comm 2>/dev/null; done";
  FILE *p = popen(cmd, "r");
  if (!p) return 0;
  char line[128];
  int only_mixer = 1, any = 0;
  while (fgets(line, sizeof line, p)) {
    line[strcspn(line, "\r\n")] = 0;
    if (!line[0]) continue;
    any = 1;
    if (strcmp(line, "mixer") != 0) only_mixer = 0;
  }
  pclose(p);
  return any && only_mixer;
}

static int wait_d24_closed(int ms) {
  for (int i = 0; i < ms; i += 100) {
    if (read_file_contains("/proc/asound/card0/pcm24c/sub0/status", "closed")) return 1;
    usleep(100 * 1000);
  }
  return read_file_contains("/proc/asound/card0/pcm24c/sub0/status", "closed");
}

static void verify_recovery(void) {
  for (int i = 0; i < 100; i++) {
    if (getprop_equals("init.svc.mixer", "running") && getprop_equals("audio_mixer.init", "true")) break;
    usleep(100 * 1000);
  }
  fprintf(stderr, "RECOVERY mixer=%s audio_mixer.init=%s d24_running=%d\n",
          getprop_equals("init.svc.mixer", "running") ? "running" : "NOT-running",
          getprop_equals("audio_mixer.init", "true") ? "true" : "NOT-true",
          read_file_contains("/proc/asound/card0/pcm24c/sub0/status", "RUNNING"));
}

static void start_mixer(void) {
  if (!g_mixer_touched) return;
  system("start mixer");
  verify_recovery();
  g_mixer_touched = 0;
}

static void ring_push(const int16_t *src, int n) {
  if (n <= 0) return;
  pthread_mutex_lock(&g_ring_lock);
  int freecap = g_ring_cap - g_ring_count;
  if (n > freecap) {
    int drop = n - freecap;
    g_ring_tail = (g_ring_tail + drop) % g_ring_cap;
    g_ring_count -= drop;
    g_dropped_frames += (unsigned long long)drop;
    g_ev_drop++;
  }
  for (int i = 0; i < n; i++) {
    g_ring[g_ring_head] = src[i];
    g_ring_head = (g_ring_head + 1) % g_ring_cap;
  }
  g_ring_count += n;
  if (g_ring_count > g_q_max) g_q_max = g_ring_count;
  if (g_ring_count > g_high) {
    int drop = g_ring_count - g_target;
    g_ring_tail = (g_ring_tail + drop) % g_ring_cap;
    g_ring_count -= drop;
    g_trim_frames += (unsigned long long)drop;
    g_trims++;
    g_ev_trim++;
  }
  pthread_cond_signal(&g_ring_cond);
  pthread_mutex_unlock(&g_ring_lock);
}

static int ring_pop(int16_t *dst, int n) {
  pthread_mutex_lock(&g_ring_lock);
  int m = g_ring_count < n ? g_ring_count : n;
  for (int i = 0; i < m; i++) {
    dst[i] = g_ring[g_ring_tail];
    g_ring_tail = (g_ring_tail + 1) % g_ring_cap;
  }
  g_ring_count -= m;
  g_q_depth = g_ring_count;
  pthread_mutex_unlock(&g_ring_lock);
  if (m < n) memset(dst + m, 0, (size_t)(n - m) * 2);
  return m;
}

static void ring_flush(void) {
  pthread_mutex_lock(&g_ring_lock);
  g_ring_head = g_ring_tail = g_ring_count = 0;
  g_q_depth = 0;
  pthread_mutex_unlock(&g_ring_lock);
}

static int dl_source_get(int16_t *dst, int n) {
  if (g_mode == 2) { /* synthetic digital silence (Phase 5D.6A D23 tests) */
    memset(dst, 0, (size_t)n * 2);
    return n;
  }
  if (g_mode == 1) {
    for (int i = 0; i < n; i++) {
      dst[i] = g_local[g_local_pos];
      g_local_pos++;
      if (g_local_pos >= g_local_len) g_local_pos = 0;
    }
    return n;
  }
  int got = ring_pop(dst, n);
  if (got < n) { g_underruns++; g_underrun_frames += (unsigned long long)(n - got); g_ev_underrun++; }
  return got;
}

static void lin_compact(void) {
  if (g_lin_head > 0) {
    memmove(g_lin, g_lin + g_lin_head, (size_t)g_lin_len * 2);
    g_lin_head = 0;
  }
}

static void lin_refill(int need) {
  if (need > LIN_CAP - 8) need = LIN_CAP - 8;
  while (g_lin_len < need) {
    if (g_lin_head + g_lin_len >= LIN_CAP) lin_compact();
    int space = LIN_CAP - (g_lin_head + g_lin_len);
    if (space <= 0) break;
    int want = need - g_lin_len;
    if (want > DL_CHUNK) want = DL_CHUNK;
    if (want > space) want = space;
    static int16_t tmp[DL_CHUNK];
    int got = dl_source_get(tmp, want);
    memcpy(g_lin + g_lin_head + g_lin_len, tmp, (size_t)got * 2);
    g_lin_len += got;
    if (got < want) break;
  }
}

static void resample_chunk(int16_t *out, double step) {
  double x = g_lin_frac;
  int need = (int)floor(x + (DL_CHUNK - 1) * step) + 2;
  lin_refill(need);
  int avail = g_lin_len;
  for (int j = 0; j < DL_CHUNK; j++) {
    double pos = x + j * step;
    int i = (int)pos;
    double f = pos - i;
    int a = (i < avail) ? g_lin[g_lin_head + i] : 0;
    int b = (i + 1 < avail) ? g_lin[g_lin_head + i + 1] : a;
    out[j] = (int16_t)lrint(a * (1.0 - f) + b * f);
  }
  double consumed = x + DL_CHUNK * step;
  int drop = (int)floor(consumed);
  if (drop > g_lin_len) drop = g_lin_len;
  g_lin_head += drop;
  g_lin_len -= drop;
  g_lin_frac = consumed - drop;
  if (g_lin_head > LIN_CAP / 2) lin_compact();
}

static void qlog_line(double t, int q_frames, int got, unsigned long long ev_u,
                      unsigned long long ev_d, unsigned long long ev_t, double step) {
  if (!g_qlog) return;
  fprintf(g_qlog, "%.3f,%d,%.1f,%d,%llu,%llu,%llu,%.0f\n",
          t * 1000.0, q_frames, q_frames * 1000.0 / DL_SR, got,
          ev_u, ev_d, ev_t, (step - 1.0) * 1e6);
}

static struct mixer_ctl *mixer_ctl_by_name(struct mixer *m, const char *name) {
  int n = mixer_get_num_ctls(m);
  for (int i = 0; i < n; i++) {
    struct mixer_ctl *c = mixer_get_ctl(m, i);
    if (c && strcmp(mixer_ctl_get_name(c), name) == 0) return c;
  }
  return NULL;
}

static void amp_set(int on) {
  if (g_amp_ctl) mixer_ctl_set_value(g_amp_ctl, 0, on ? 1 : 0);
}

static void gate_init(void) {
  g_mix = mixer_open(0);
  if (!g_mix) { fprintf(stderr, "[gate] mixer_open failed\n"); return; }
  g_amp_ctl = mixer_ctl_by_name(g_mix, "Ext_Speaker_Amp_Switch");
  if (!g_amp_ctl) { fprintf(stderr, "[gate] Ext_Speaker_Amp_Switch not found\n"); return; }
  fprintf(stderr, "[gate] amp control found gate=%d gate_ms=%d settle_ms=%d fade_ms=%d\n",
          g_gate, g_gate_ms, g_settle_ms, g_fade_ms);
}

static void pb_account(const int16_t *st) {
  long acc = 0; int peak = 0; unsigned long long nz = 0;
  for (int i = 0; i < DL_CHUNK * 2; i++) {
    int v = st[i]; int a = v < 0 ? -v : v;
    acc += (long)v * v; if (a > peak) peak = a; if (v != 0) nz++;
  }
  g_pb_rms_acc += acc; g_pb_peak_acc = peak > g_pb_peak_acc ? peak : g_pb_peak_acc;
  g_pb_nz_acc += nz; g_pb_n_acc += DL_CHUNK * 2;
  if (g_pbraw) fwrite(st, 2, DL_CHUNK * 2, g_pbraw);
}

static void gate_apply(int16_t *st, int all_zero) {
  if (!g_gate || !g_amp_ctl) return;
  if (all_zero) {
    g_silent_ms += 10;
    if (g_amp_on && g_silent_ms >= g_gate_ms) {
      amp_set(0); g_amp_on = 0; g_gate_offs++;
      fprintf(stderr, "[gate] amp OFF after %ldms silence (offs=%llu)\n", g_silent_ms, g_gate_offs);
    }
    return;
  }
  g_silent_ms = 0;
  if (!g_amp_on) {
    amp_set(1); g_amp_on = 1; g_gate_ons++;
    fprintf(stderr, "[gate] amp ON -> playback (ons=%llu)\n", g_gate_ons);
    int fade = g_fade_ms * DL_SR / 1000;
    if (fade > DL_CHUNK) fade = DL_CHUNK;
    for (int i = 0; i < fade; i++) {
      int g = (i * 256) / (fade > 0 ? fade : 1);
      st[2 * i] = (int16_t)((st[2 * i] * g) >> 8);
      st[2 * i + 1] = (int16_t)((st[2 * i + 1] * g) >> 8);
    }
  }
}

static void *playback_thread(void *arg) {
  (void)arg;
  int16_t mono[DL_CHUNK];
  int16_t st[DL_CHUNK * 2];

  if (g_mode == 0) {
    while (!g_stop) {
      int avail;
      pthread_mutex_lock(&g_ring_lock);
      avail = g_ring_count;
      pthread_mutex_unlock(&g_ring_lock);
      if (avail >= g_prebuf) break;
      memset(st, 0, sizeof st);
      gate_apply(st, 1);
      if (g_pb) pcm_write(g_pb, st, sizeof st);
      usleep(2000);
    }
  }

  for (int k = 0; k < 2 && !g_stop; k++) {
    int got = dl_source_get(mono, DL_CHUNK);
    (void)got;
    for (int i = 0; i < DL_CHUNK; i++) { st[2 * i] = mono[i]; st[2 * i + 1] = mono[i]; }
    gate_apply(st, 1);
    pb_account(st);
    if (g_pb && pcm_write(g_pb, st, sizeof st) != 0) { g_pb_errs++; g_ev_pb_err++; pcm_prepare(g_pb); g_ev_prepare++; }
  }

  if (g_pb_open_only) {
    /* Phase 5D.6A Test B: D23 open + primed, then NO continuous writes. */
    while (!g_stop) usleep(20000);
    return NULL;
  }

  struct timespec start;
  clock_gettime(CLOCK_MONOTONIC, &start);
  unsigned long long writes = 0;
  double t0 = now_s();
  unsigned long long last_u = 0, last_d = 0, last_t = 0;

  while (!g_stop) {
    int q_frames;
    pthread_mutex_lock(&g_ring_lock);
    q_frames = (g_mode == 0) ? g_ring_count : 0;
    pthread_mutex_unlock(&g_ring_lock);

    if (g_mode != 1 && q_frames < g_low && !g_stop) {
      memset(st, 0, sizeof st);
      gate_apply(st, 1);
      pb_account(st);
      if (g_pb) pcm_write(g_pb, st, sizeof st);
      g_underruns++;
      g_underrun_frames += DL_CHUNK;
      g_ev_underrun++;
      g_ev_zero_underrun++;
      double tr = now_s() - t0;
      qlog_line(tr, q_frames, 0, g_ev_underrun - last_u, g_ev_drop - last_d, g_ev_trim - last_t, 0.0);
      last_u = g_ev_underrun; last_d = g_ev_drop; last_t = g_ev_trim;
      usleep(3000);
      continue;
    }
    double step = 1.0;
    int got;
    if (g_drift == 1) {
      double corr;
      if (g_force_ppm != 0) {
        corr = g_force_ppm / 1e6;
      } else {
        double err = g_mode == 0 ? (double)(q_frames - g_target) / (double)(g_target > 0 ? g_target : 1) : 0.0;
        corr = err * 0.02;
        if (corr > 0.005) corr = 0.005;
        if (corr < -0.005) corr = -0.005;
      }
      step = 1.0 + corr;
      g_last_step = step;
      g_step_ppm = corr * 1e6;
      resample_chunk(mono, step);
      got = DL_CHUNK;
      if (step != 1.0) g_resample_corrections++;
    } else {
      got = dl_source_get(mono, DL_CHUNK);
    }

    for (int i = 0; i < DL_CHUNK; i++) { st[2 * i] = mono[i]; st[2 * i + 1] = mono[i]; }
    int allz = 1;
    for (int i = 0; i < DL_CHUNK; i++) if (mono[i] != 0) { allz = 0; break; }
    if (allz) g_ev_zero_data++;
    gate_apply(st, allz);
    pb_account(st);
    if (g_pb) {
      struct timespec a, b;
      clock_gettime(CLOCK_MONOTONIC, &a);
      int rc = pcm_write(g_pb, st, sizeof st);
      clock_gettime(CLOCK_MONOTONIC, &b);
      g_pb_ns += (unsigned long long)((b.tv_sec - a.tv_sec) * 1000000000LL + (b.tv_nsec - a.tv_nsec));
      g_pb_writes++;
      if (rc != 0) { g_pb_errs++; g_ev_pb_err++; pcm_prepare(g_pb); g_ev_prepare++; }
    }
    writes++;

    double t = now_s() - t0;
    qlog_line(t, q_frames, got, g_ev_underrun - last_u, g_ev_drop - last_d, g_ev_trim - last_t, g_last_step);
    last_u = g_ev_underrun; last_d = g_ev_drop; last_t = g_ev_trim;

    if (g_drift == 0) {
      unsigned long long expected = (unsigned long long)(t * DL_SR / DL_CHUNK);
      int guard = 0;
      while (writes < expected && !g_stop && guard < 8) {
        got = dl_source_get(mono, DL_CHUNK);
        (void)got;
        for (int i = 0; i < DL_CHUNK; i++) { st[2 * i] = mono[i]; st[2 * i + 1] = mono[i]; }
        { int az = 1; for (int i = 0; i < DL_CHUNK; i++) if (mono[i] != 0) { az = 0; break; } gate_apply(st, az); }
        pb_account(st);
        if (g_pb && pcm_write(g_pb, st, sizeof st) != 0) { g_pb_errs++; g_ev_pb_err++; pcm_prepare(g_pb); g_ev_prepare++; }
        writes++;
        guard++;
        g_ev_catchup++;
      }
    }
  }
  return NULL;
}

static void *recv_thread(void *arg) {
  (void)arg;
  unsigned char hdr[10];
  while (!g_stop) {
    size_t got = 0;
    while (got < sizeof hdr && !g_stop) {
      ssize_t r = recv(g_sock, hdr + got, sizeof hdr - got, 0);
      if (r == 0) { g_stop = 1; return NULL; }
      if (r < 0) { if (errno == EINTR) continue; g_stop = 1; return NULL; }
      got += (size_t)r;
    }
    uint16_t magic = (uint16_t)(hdr[0] | (hdr[1] << 8));
    uint8_t type = hdr[2];
    uint32_t seq = (uint32_t)hdr[4] | ((uint32_t)hdr[5] << 8) | ((uint32_t)hdr[6] << 16) | ((uint32_t)hdr[7] << 24);
    uint16_t len = (uint16_t)(hdr[8] | (hdr[9] << 8));
    if (magic != MAGIC) { g_stop = 1; return NULL; }
    unsigned char *payload = NULL;
    if (len) {
      payload = malloc(len);
      if (!payload) { g_stop = 1; return NULL; }
      size_t p = 0;
      while (p < len && !g_stop) {
        ssize_t r = recv(g_sock, payload + p, len - p, 0);
        if (r <= 0) { free(payload); g_stop = 1; return NULL; }
        p += (size_t)r;
      }
    }
    if (type == T_HELLO) {
      uint64_t mono;
      now_mono_us(&mono);
      unsigned char out[18];
      uint16_t m = MAGIC;
      out[0] = m & 0xff; out[1] = m >> 8; out[2] = T_HELLO_ACK; out[3] = 0;
      out[4] = 0; out[5] = 0; out[6] = 0; out[7] = 0; out[8] = 8; out[9] = 0;
      for (int i = 0; i < 8; i++) out[10 + i] = (mono >> (8 * i)) & 0xff;
      pthread_mutex_lock(&g_tx);
      send(g_sock, out, sizeof out, MSG_NOSIGNAL);
      pthread_mutex_unlock(&g_tx);
    } else if (type == T_DOWNLINK) {
      if (len >= 2) {
        int n = len / 2;
        if (g_dl_have_last) {
          uint32_t expected = (g_dl_last + 1) & 0xFFFFFFFF;
          uint32_t delta = (seq - expected) & 0xFFFFFFFF;
          if (seq == expected) {
          } else if (seq == g_dl_last) g_dl_dupes++;
          else if (delta < 0x80000000) g_dl_gaps += delta;
        }
        g_dl_last = seq;
        g_dl_have_last = 1;
        g_dl_recv++;
        if (g_rlog) {
          double tn = now_s();
          fprintf(g_rlog, "%.6f,%u,%d\n", tn, seq, g_ring_count);
        }
        ring_push((const int16_t *)payload, n);
      }
    } else if (type == T_FLUSH || type == T_RESET) {
      ring_flush();
    }
    free(payload);
  }
  return NULL;
}

static int send_all(const unsigned char *buf, size_t len) {
  size_t off = 0;
  pthread_mutex_lock(&g_tx);
  while (off < len) {
    ssize_t n = send(g_sock, buf + off, len - off, MSG_NOSIGNAL);
    if (n < 0) {
      if (errno == EINTR) continue;
      pthread_mutex_unlock(&g_tx);
      return (errno == EAGAIN || errno == EWOULDBLOCK) ? (off == 0 ? 1 : -1) : -1;
    }
    off += (size_t)n;
  }
  pthread_mutex_unlock(&g_tx);
  return 0;
}

static int send_uplink(uint64_t capture_us, const int16_t *pcm, int n) {
  if (g_no_uplink) return 0;
  if (g_mode != 0 || g_sock < 0) return 0;
  unsigned char frame[10 + 8 + AEC_FRAME * 2];
  uint16_t magic = MAGIC;
  uint16_t len = (uint16_t)(8 + n * 2);
  frame[0] = magic & 0xff; frame[1] = magic >> 8;
  frame[2] = T_UPLINK; frame[3] = 0;
  uint32_t seq = (uint32_t)g_frames_sent++;
  frame[4] = seq & 0xff; frame[5] = (seq >> 8) & 0xff; frame[6] = (seq >> 16) & 0xff; frame[7] = (seq >> 24) & 0xff;
  frame[8] = len & 0xff; frame[9] = len >> 8;
  for (int i = 0; i < 8; i++) frame[10 + i] = (capture_us >> (8 * i)) & 0xff;
  memcpy(frame + 18, pcm, (size_t)n * 2);
  int rc = send_all(frame, 10 + len);
  if (rc < 0) return -1;
  if (rc == 1) g_tx_drops++;
  return 0;
}

static int send_meter(void) {
  if (g_mode != 0 || g_sock < 0) return 0;
  unsigned char frame[10 + 48];
  uint16_t magic = MAGIC;
  frame[0] = magic & 0xff; frame[1] = magic >> 8;
  frame[2] = T_METER; frame[3] = 0;
  frame[4] = 0; frame[5] = 0; frame[6] = 0; frame[7] = 0;
  frame[8] = 48; frame[9] = 0;
  int32_t erle = (g_sum_out > 0.0) ? (int32_t)(10.0 * log10((g_sum_in + 1e-9) / (g_sum_out + 1e-9)) * 1000.0) : 0;
  uint32_t v[12];
  v[0] = (uint32_t)g_cap_errs; v[1] = (uint32_t)g_frames_sent; v[2] = (uint32_t)erle;
  v[3] = (uint32_t)g_dl_recv; v[4] = (uint32_t)g_dl_gaps; v[5] = (uint32_t)g_dl_dupes;
  v[6] = (uint32_t)g_underruns; v[7] = (uint32_t)g_dropped_frames; v[8] = (uint32_t)g_q_depth;
  v[9] = (uint32_t)g_q_max; v[10] = (uint32_t)g_ref_rms; v[11] = (uint32_t)g_out_rms;
  memcpy(frame + 10, v, sizeof v);
  return send_all(frame, sizeof frame);
}

static int connect_server(const char *host, int port) {
  char portstr[16];
  snprintf(portstr, sizeof portstr, "%d", port);
  struct addrinfo hints, *res = NULL;
  memset(&hints, 0, sizeof hints);
  hints.ai_family = AF_INET;
  hints.ai_socktype = SOCK_STREAM;
  if (getaddrinfo(host, portstr, &hints, &res) != 0) return -1;
  int s = socket(AF_INET, SOCK_STREAM, 0);
  if (s < 0) { freeaddrinfo(res); return -1; }
  struct timeval tv = {0, 500 * 1000};
  setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
  int b = 1 << 20;
  setsockopt(s, SOL_SOCKET, SO_SNDBUF, &b, sizeof b);
  setsockopt(s, SOL_SOCKET, SO_RCVBUF, &b, sizeof b);
  int one = 1;
  setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
  if (connect(s, res->ai_addr, res->ai_addrlen) != 0) { close(s); freeaddrinfo(res); return -1; }
  freeaddrinfo(res);
  return s;
}

static uint16_t rd16(const unsigned char *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t rd32(const unsigned char *p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }

static int load_wav48_mono(const char *path) {
  FILE *f = fopen(path, "rb");
  if (!f) return -1;
  unsigned char hdr[12];
  if (fread(hdr, 1, 12, f) != 12 || memcmp(hdr, "RIFF", 4) || memcmp(hdr + 8, "WAVE", 4)) { fclose(f); return -1; }
  int fmt = 0, ch = 0, bits = 0, rate = 0, data_bytes = 0;
  unsigned char ck[8];
  while (fread(ck, 1, 8, f) == 8) {
    uint32_t sz = rd32(ck + 4);
    if (!memcmp(ck, "fmt ", 4)) {
      unsigned char b[16];
      if (sz < 16 || fread(b, 1, 16, f) != 16) { fclose(f); return -1; }
      fmt = rd16(b); ch = rd16(b + 2); rate = rd32(b + 4); bits = rd16(b + 14);
      if (sz > 16) fseek(f, sz - 16, SEEK_CUR);
    } else if (!memcmp(ck, "data", 4)) {
      data_bytes = (int)sz;
      break;
    } else {
      fseek(f, sz + (sz & 1), SEEK_CUR);
    }
  }
  if (fmt != 1 || bits != 16 || ch < 1 || rate < 1 || data_bytes <= 0) { fclose(f); return -1; }
  int frames = data_bytes / (2 * ch);
  int16_t *src = malloc((size_t)frames * ch * 2);
  if (!src) { fclose(f); return -1; }
  fread(src, 2, (size_t)frames * ch, f);
  fclose(f);
  int outn = (int)((long long)frames * DL_SR / rate);
  g_local = malloc((size_t)outn * 2);
  for (int i = 0; i < outn; i++) {
    double x = (double)i * rate / DL_SR;
    int i0 = (int)x;
    int i1 = i0 + 1 < frames ? i0 + 1 : frames - 1;
    double fr = x - i0;
    int acc = 0;
    for (int c = 0; c < ch; c++) {
      double a = src[(size_t)i0 * ch + c], b = src[(size_t)i1 * ch + c];
      acc += (int)(a * (1 - fr) + b * fr);
    }
    g_local[i] = (int16_t)(acc / ch);
  }
  g_local_len = outn;
  free(src);
  return 0;
}

static void cleanup(void) {
  g_stop = 1;
  pthread_cond_broadcast(&g_ring_cond);
  if (g_play_started) { pthread_join(g_play_th, NULL); g_play_started = 0; }
  if (g_sock >= 0) { shutdown(g_sock, SHUT_RDWR); close(g_sock); g_sock = -1; }
  if (g_rx_started) { pthread_join(g_rx_th, NULL); g_rx_started = 0; }
  if (g_pb) { pcm_close(g_pb); g_pb = NULL; }
  if (g_cap) { pcm_close(g_cap); g_cap = NULL; }
  if (g_st) { speex_echo_state_destroy(g_st); g_st = NULL; }
  if (g_qlog) { fclose(g_qlog); g_qlog = NULL; }
  if (g_rlog) { fclose(g_rlog); g_rlog = NULL; }
  if (g_refraw) { fclose(g_refraw); g_refraw = NULL; }
  if (g_ref8raw) { fclose(g_ref8raw); g_ref8raw = NULL; }
  if (g_pbraw) { fclose(g_pbraw); g_pbraw = NULL; }
  if (g_micraw) { fclose(g_micraw); g_micraw = NULL; }
  if (g_outraw) { fclose(g_outraw); g_outraw = NULL; }
  free(g_ring); g_ring = NULL;
  free(g_local); g_local = NULL;
  if (g_amp_ctl && !g_amp_on) { amp_set(1); g_amp_on = 1; }
  if (g_mix) { mixer_close(g_mix); g_mix = NULL; g_amp_ctl = NULL; }
  start_mixer();
}

int main(int argc, char **argv) {
  if (argc < 3) {
    fprintf(stderr, "usage:\n  echo_live net <ip> <port> <secs>\n  echo_live local <wav> <secs>\n");
    return 2;
  }
  signal(SIGINT, on_sig);
  signal(SIGTERM, on_sig);
  signal(SIGPIPE, SIG_IGN);

  double secs;
  if (!strcmp(argv[1], "net")) {
    if (argc < 5) { fprintf(stderr, "net needs <ip> <port> <secs>\n"); return 2; }
    g_mode = 0;
    snprintf(g_server, sizeof g_server, "%s", argv[2]);
    g_port = atoi(argv[3]);
    secs = atof(argv[4]);
  } else if (!strcmp(argv[1], "local")) {
    if (argc < 4) { fprintf(stderr, "local needs <wav> <secs>\n"); return 2; }
    g_mode = 1;
    secs = atof(argv[3]);
    if (load_wav48_mono(argv[2]) != 0) { fprintf(stderr, "wav load failed: %s\n", argv[2]); return 2; }
    fprintf(stderr, "local source: %s frames=%ld (%.1fs)\n", argv[2], g_local_len, g_local_len / 48000.0);
  } else if (!strcmp(argv[1], "zero")) {
    /* Phase 5D.6A: synthetic digital silence, no network, isolated D23 tests. */
    if (argc < 3) { fprintf(stderr, "zero needs <secs>\n"); return 2; }
    g_mode = 2;
    secs = atof(argv[2]);
  } else {
    fprintf(stderr, "unknown mode %s\n", argv[1]);
    return 2;
  }

  g_prebuf = getenv_int("EL_PREBUF_MS", 60) * DL_SR / 1000;
  g_high = getenv_int("EL_HIGH_MS", 90) * DL_SR / 1000;
  g_target = getenv_int("EL_TARGET_MS", 60) * DL_SR / 1000;
  g_low = getenv_int("EL_LOW_MS", 30) * DL_SR / 1000;
  g_ring_cap = getenv_int("EL_CAP_MS", 100) * DL_SR / 1000;
  if (g_ring_cap < g_high + DL_CHUNK) g_ring_cap = g_high + DL_CHUNK;
  g_drift = getenv_int("EL_DRIFT", 1);
  g_force_ppm = getenv_int("EL_FORCE_PPM", 0);
  const char *q = getenv("EL_QLOG");
  if (q && *q) g_qlog = fopen(q, "w");
  const char *rl = getenv("EL_RLOG");
  if (rl && *rl) g_rlog = fopen(rl, "w");
  const char *rr = getenv("EL_REFRAW");
  if (rr && *rr) g_refraw = fopen(rr, "wb");
  const char *mr = getenv("EL_MICRAW");
  if (mr && *mr) g_micraw = fopen(mr, "wb");
  const char *orw = getenv("EL_OUTRAW");
  if (orw && *orw) g_outraw = fopen(orw, "wb");
  const char *r8w = getenv("EL_REF8RAW");
  if (r8w && *r8w) g_ref8raw = fopen(r8w, "wb");
  const char *pbw = getenv("EL_PBRAW");
  if (pbw && *pbw) g_pbraw = fopen(pbw, "wb");
  const char *pbm = getenv("EL_PB_MODE");
  g_pb_open_only = (pbm && !strcmp(pbm, "open_only")) ? 1 : 0;
  g_gate = getenv_int("EL_GATE", 0);
  g_gate_ms = getenv_int("EL_GATE_MS", 1200);
  g_settle_ms = getenv_int("EL_SETTLE_MS", 12);
  g_fade_ms = getenv_int("EL_FADE_MS", 8);
  g_no_uplink = getenv_int("EL_NO_UPLINK", 0);
  g_up_every = getenv_int("EL_UP_EVERY", 1);

  g_ring = malloc((size_t)g_ring_cap * 2);
  if (!g_ring) return 1;

  if (!getprop_equals("init.svc.mixer", "running") || !d24_owner_is_mixer_only()) {
    fprintf(stderr, "preflight failed (mixer not running or D24 owner != mixer)\n");
    free(g_ring); return 3;
  }
  system("stop mixer");
  g_mixer_touched = 1;
  if (!wait_d24_closed(5000)) { fprintf(stderr, "D24 did not close\n"); start_mixer(); return 5; }

  struct pcm_config cc;
  memset(&cc, 0, sizeof cc);
  cc.channels = CHANS; cc.rate = 16000; cc.period_size = CAP_PERIOD; cc.period_count = CAP_COUNT; cc.format = 4;
  g_cap = pcm_open(0, 24, PCM_IN, &cc);
  if (!g_cap || !pcm_is_ready(g_cap)) { fprintf(stderr, "D24 open failed\n"); cleanup(); return 6; }

  struct pcm_config pc;
  memset(&pc, 0, sizeof pc);
  pc.channels = 2; pc.rate = DL_SR; pc.period_size = DL_CHUNK; pc.period_count = 4; pc.format = 0;
  g_pb = pcm_open(0, 23, PCM_OUT, &pc);
  if (!g_pb || !pcm_is_ready(g_pb)) { fprintf(stderr, "D23 open failed\n"); cleanup(); return 7; }
  g_alsa_buf_frames = (int)pcm_get_buffer_size(g_pb);
  {
    int16_t sil[DL_CHUNK * 2];
    memset(sil, 0, sizeof sil);
    for (int k = 0; k < 4; k++) if (pcm_write(g_pb, sil, sizeof sil) != 0) pcm_prepare(g_pb);
  }
  if (g_gate) {
    gate_init();
    if (g_amp_ctl) { amp_set(0); g_amp_on = 0; }
  }
  fprintf(stderr, "cfg mode=%s prebuf=%dms high=%dms target=%dms cap=%dms drift=%s alsa_buf=%d\n",
          g_mode ? "local" : "net", g_prebuf * 1000 / DL_SR, g_high * 1000 / DL_SR,
          g_target * 1000 / DL_SR, g_ring_cap * 1000 / DL_SR, g_drift ? "resample" : "discrete",
          g_alsa_buf_frames);

  g_st = speex_echo_state_init(AEC_FRAME, AEC_TAIL);
  int32_t rate = 16000;
  speex_echo_ctl(g_st, SPEEX_ECHO_SET_SAMPLING_RATE, &rate);

  if (g_mode == 0) {
    g_sock = -1;
    for (int a = 0; a < 10 && g_sock < 0 && !g_stop; a++) {
      g_sock = connect_server(g_server, g_port);
      if (g_sock < 0) { fprintf(stderr, "connect retry %d\n", a + 1); usleep(500 * 1000); }
    }
    if (g_sock < 0) { cleanup(); return 8; }
  }

  pthread_create(&g_play_th, NULL, playback_thread, NULL);
  g_play_started = 1;
  if (g_mode == 0) { pthread_create(&g_rx_th, NULL, recv_thread, NULL); g_rx_started = 1; }

  size_t per_bytes = (size_t)cc.period_size * cc.period_count * CHANS * 3;
  int block_frames = (int)(cc.period_size * cc.period_count);
  unsigned char *pr = malloc(per_bytes);
  int16_t *mic_all = malloc((size_t)(block_frames + AEC_FRAME) * 2);
  int16_t *ref_all = malloc((size_t)(block_frames + AEC_FRAME) * 2);
  int16_t *ref8_all = malloc((size_t)(block_frames + AEC_FRAME) * 2);
  int16_t mic[AEC_FRAME], ref[AEC_FRAME], ref8[AEC_FRAME], out[AEC_FRAME];
  int carry = 0;
  double t_start = now_s();
  double last_meter = 0.0, win_ref = 0.0, win_out = 0.0;
  unsigned long win_n = 0;

  while (!g_stop) {
    double el = now_s() - t_start;
    if (secs > 0 && el >= secs) break;
    if (pcm_read(g_cap, pr, per_bytes) != 0) { g_cap_errs++; if (g_cap_errs > 50) break; continue; }
    uint64_t cap_us;
    now_mono_us(&cap_us);
    int idx = carry;
    for (int i = 0; i < block_frames; i++) {
      unsigned char *m = pr + (size_t)i * CHANS * 3;
      unsigned char *r = pr + (size_t)i * CHANS * 3 + REF_CH * 3;
      unsigned char *r8 = pr + (size_t)i * CHANS * 3 + (REF_CH + 1) * 3;
      int32_t vm = m[0] | (m[1] << 8) | (m[2] << 16);
      int32_t vr = r[0] | (r[1] << 8) | (r[2] << 16);
      int32_t v8 = r8[0] | (r8[1] << 8) | (r8[2] << 16);
      if (vm & 0x800000) vm |= ~0xFFFFFF;
      if (vr & 0x800000) vr |= ~0xFFFFFF;
      if (v8 & 0x800000) v8 |= ~0xFFFFFF;
      mic_all[idx + i] = (int16_t)(vm >> 8);
      ref_all[idx + i] = (int16_t)(vr >> 8);
      ref8_all[idx + i] = (int16_t)(v8 >> 8);
    }
    int total = idx + block_frames;
    int nfr = total / AEC_FRAME;
    for (int f = 0; f < nfr; f++) {
      memcpy(mic, mic_all + f * AEC_FRAME, AEC_FRAME * 2);
      memcpy(ref, ref_all + f * AEC_FRAME, AEC_FRAME * 2);
      memcpy(ref8, ref8_all + f * AEC_FRAME, AEC_FRAME * 2);
      speex_echo_cancellation(g_st, mic, ref, out);
      for (int i = 0; i < AEC_FRAME; i++) {
        double a = mic[i], b = out[i], c = ref[i];
        g_sum_in += a * a; g_sum_out += b * b; win_ref += c * c; win_out += b * b;
      }
      win_n += AEC_FRAME;
      if (g_micraw) fwrite(mic, 2, AEC_FRAME, g_micraw);
      if (g_refraw) fwrite(ref, 2, AEC_FRAME, g_refraw);
      if (g_ref8raw) fwrite(ref8, 2, AEC_FRAME, g_ref8raw);
      if (g_outraw) fwrite(out, 2, AEC_FRAME, g_outraw);
      g_aec_frames++;
      if ((g_aec_frames % (g_up_every > 0 ? g_up_every : 1)) == 0) {
        if (send_uplink(cap_us, out, AEC_FRAME) != 0) { g_stop = 1; break; }
      }
    }
    int consumed = nfr * AEC_FRAME;
    carry = total - consumed;
    if (carry > 0) {
      memmove(mic_all, mic_all + consumed, (size_t)carry * 2);
      memmove(ref_all, ref_all + consumed, (size_t)carry * 2);
      memmove(ref8_all, ref8_all + consumed, (size_t)carry * 2);
    }
    if (el - last_meter >= 1.0) {
      last_meter = el;
      g_ref_rms = win_n ? (int)sqrt(win_ref / win_n) : 0;
      g_out_rms = win_n ? (int)sqrt(win_out / win_n) : 0;
      win_ref = win_out = 0.0; win_n = 0;
      g_pb_rms = g_pb_n_acc ? (int)sqrt((double)g_pb_rms_acc / (double)g_pb_n_acc) : 0;
      g_pb_peak = g_pb_peak_acc;
      g_pb_nonzero = g_pb_nz_acc; g_pb_samples = g_pb_n_acc;
      g_pb_rms_acc = 0; g_pb_peak_acc = 0; g_pb_nz_acc = 0; g_pb_n_acc = 0;
      send_meter();
      double erle = g_sum_out > 0.0 ? 10.0 * log10((g_sum_in + 1e-9) / (g_sum_out + 1e-9)) : 0.0;
      fprintf(stderr, "t=%.1fs up=%llu errs=%d erle=%.2f ref=%d out=%d | dl=%llu gaps=%llu dup=%llu ue=%llu uf=%llu drop=%llu trim=%llu q=%d qmax=%d step=%.0fppm pbw=%llu | pbrms=%d pbpeak=%d pbnz=%llu/%llu zwe=%llu zdata=%llu pberr=%llu prep=%llu catch=%llu\n",
              el, g_frames_sent, g_cap_errs, erle, g_ref_rms, g_out_rms,
              g_dl_recv, g_dl_gaps, g_dl_dupes, g_underruns, g_underrun_frames,
              g_dropped_frames, g_trim_frames, g_q_depth, g_q_max, g_step_ppm, g_pb_writes,
              g_pb_rms, g_pb_peak, g_pb_nonzero, g_pb_samples,
              g_ev_zero_underrun, g_ev_zero_data, g_ev_pb_err, g_ev_prepare, g_ev_catchup);
    }
  }

  g_stop = 1;
  pthread_cond_broadcast(&g_ring_cond);
  if (g_sock >= 0) shutdown(g_sock, SHUT_RDWR);
  cleanup();
  double erle = g_sum_out > 0.0 ? 10.0 * log10((g_sum_in + 1e-9) / (g_sum_out + 1e-9)) : 0.0;
  struct rusage ru;
  getrusage(RUSAGE_SELF, &ru);
  double cpu = (ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6) + (ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6);
  double wall = now_s() - t_start;
  fprintf(stderr, "DONE mode=%s erle=%.2f | dl=%llu gaps=%llu dupes=%llu underrun_ev=%llu underrun_fr=%llu dropped_fr=%llu trims=%llu trimfr=%llu qmax=%d resample_corr=%llu | cpu=%.2fs/%.1fs (%.1f%%) maxrss=%ldKB | gate offs=%llu ons=%llu\n",
          g_mode ? "local" : "net", erle, g_dl_recv, g_dl_gaps, g_dl_dupes,
          g_underruns, g_underrun_frames, g_dropped_frames, g_trims, g_trim_frames,
          g_q_max, g_resample_corrections, cpu, wall, wall > 0 ? cpu / wall * 100.0 : 0.0, ru.ru_maxrss,
          g_gate_offs, g_gate_ons);
  free(pr); free(mic_all); free(ref_all); free(ref8_all);
  return 0;
}
