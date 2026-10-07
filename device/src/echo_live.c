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

#define MAGIC 0xE0D1
#define T_UPLINK 0x01
#define T_DOWNLINK 0x02
#define T_METER 0x03
#define T_FLUSH 0x80
#define T_BARGEIN 0x81
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
#define DL_RING_FRAMES 4800
#define DL_PRIME_CHUNKS 2
#define DL_HIGH 4320
#define DL_TARGET 2880

static volatile int g_stop = 0;
static volatile int g_playing = 1;
static int g_sock = -1;
static struct pcm *g_cap = NULL;
static struct pcm *g_pb = NULL;
static pthread_t g_play_th, g_rx_th;
static int g_play_started = 0, g_rx_started = 0;
static SpeexEchoState *g_st = NULL;
static unsigned long long g_frames_sent = 0;
static unsigned long long g_tx_drops = 0;
static int g_cap_errs = 0;
static double g_sum_in = 0.0, g_sum_out = 0.0;
static int g_mixer_touched = 0;
static char g_server[128];
static int g_port = 0;
static pthread_mutex_t g_tx = PTHREAD_MUTEX_INITIALIZER;

static int16_t *g_ring = NULL;
static int g_ring_cap = DL_RING_FRAMES;
static int g_ring_head = 0, g_ring_tail = 0, g_ring_count = 0;
static int g_prebuf = 0;
static pthread_mutex_t g_ring_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_ring_cond = PTHREAD_COND_INITIALIZER;
static volatile unsigned long long g_underruns = 0, g_dropped_frames = 0;
static volatile unsigned long long g_trims = 0, g_trim_frames = 0;
static volatile double g_dl_ia_sum = 0, g_dl_ia_sq = 0;
static volatile unsigned long long g_dl_ia_n = 0;
static volatile double g_dl_ia_max = 0;
static struct timespec g_dl_ia_last;
static int g_dl_ia_have = 0;
static volatile unsigned long long g_dl_recv = 0, g_dl_gaps = 0, g_dl_dupes = 0;
static volatile uint32_t g_dl_last = 0;
static int g_dl_have_last = 0;
static volatile int g_q_depth = 0;
static volatile int g_q_max = 0;
static volatile int g_ref_rms = 0, g_out_rms = 0;
static volatile int g_alsa_buf_frames = 0;
static volatile unsigned long long g_pb_errs = 0;
static volatile unsigned long long g_pb_writes = 0;
static volatile unsigned long long g_pb_ns = 0;

static void on_sig(int s) {
  (void)s;
  g_stop = 1;
}

static void now_mono_us(uint64_t *out) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  *out = (uint64_t)t.tv_sec * 1000000ULL + (uint64_t)t.tv_nsec / 1000ULL;
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
  if (!fgets(out, sizeof out, p)) {
    pclose(p);
    return 0;
  }
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
    if (strcmp(line, "mixer") != 0) {
      fprintf(stderr, "D24 owned by non-mixer process: %s\n", line);
      only_mixer = 0;
    }
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
    if (getprop_equals("init.svc.mixer", "running") && getprop_equals("audio_mixer.init", "true"))
      break;
    usleep(100 * 1000);
  }
  int d24 = read_file_contains("/proc/asound/card0/pcm24c/sub0/status", "RUNNING");
  fprintf(stderr, "RECOVERY mixer=%s audio_mixer.init=%s d24_running=%d\n",
          getprop_equals("init.svc.mixer", "running") ? "running" : "NOT-running",
          getprop_equals("audio_mixer.init", "true") ? "true" : "NOT-true", d24);
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
  }
  for (int i = 0; i < n; i++) {
    g_ring[g_ring_head] = src[i];
    g_ring_head = (g_ring_head + 1) % g_ring_cap;
  }
  g_ring_count += n;
  if (g_ring_count > g_q_max) g_q_max = g_ring_count;
  if (g_ring_count > DL_HIGH) {
    int drop = g_ring_count - DL_TARGET;
    g_ring_tail = (g_ring_tail + drop) % g_ring_cap;
    g_ring_count -= drop;
    g_trim_frames += (unsigned long long)drop;
    g_trims++;
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

static void *playback_thread(void *arg) {
  (void)arg;
  int16_t mono[DL_CHUNK];
  int16_t st[DL_CHUNK * 2];

  while (!g_stop) {
    int avail;
    pthread_mutex_lock(&g_ring_lock);
    avail = g_ring_count;
    pthread_mutex_unlock(&g_ring_lock);
    if (avail >= g_prebuf) break;
    memset(st, 0, sizeof st);
    if (g_pb) pcm_write(g_pb, st, sizeof st);
    usleep(2000);
  }

  for (int k = 0; k < DL_PRIME_CHUNKS && !g_stop; k++) {
    int got = ring_pop(mono, DL_CHUNK);
    if (got < DL_CHUNK) g_underruns++;
    for (int i = 0; i < DL_CHUNK; i++) {
      st[2 * i] = mono[i];
      st[2 * i + 1] = mono[i];
    }
    if (g_pb) {
      if (pcm_write(g_pb, st, sizeof st) != 0) {
        g_pb_errs++;
        pcm_prepare(g_pb);
      }
    }
  }

  struct timespec start;
  clock_gettime(CLOCK_MONOTONIC, &start);
  unsigned long long writes = 0;
  while (!g_stop) {
    int got = ring_pop(mono, DL_CHUNK);
    if (got < DL_CHUNK) g_underruns++;
    for (int i = 0; i < DL_CHUNK; i++) {
      st[2 * i] = mono[i];
      st[2 * i + 1] = mono[i];
    }
    if (g_pb) {
      struct timespec a, b;
      clock_gettime(CLOCK_MONOTONIC, &a);
      int rc = pcm_write(g_pb, st, sizeof st);
      clock_gettime(CLOCK_MONOTONIC, &b);
      g_pb_ns += (unsigned long long)((b.tv_sec - a.tv_sec) * 1000000000LL + (b.tv_nsec - a.tv_nsec));
      g_pb_writes++;
      if (rc != 0) { g_pb_errs++; pcm_prepare(g_pb); }
    }
    writes++;
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    double el = (t.tv_sec - start.tv_sec) + (t.tv_nsec - start.tv_nsec) / 1e9;
    unsigned long long expected = (unsigned long long)(el * DL_SR / DL_CHUNK);
    int guard = 0;
    while (writes < expected && !g_stop && guard < 8) {
      got = ring_pop(mono, DL_CHUNK);
      if (got < DL_CHUNK) g_underruns++;
      for (int i = 0; i < DL_CHUNK; i++) {
        st[2 * i] = mono[i];
        st[2 * i + 1] = mono[i];
      }
      if (g_pb) {
        if (pcm_write(g_pb, st, sizeof st) != 0) { g_pb_errs++; pcm_prepare(g_pb); }
      }
      writes++;
      guard++;
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
      if (r < 0) {
        if (errno == EINTR) continue;
        g_stop = 1;
        return NULL;
      }
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
          } else if (seq == g_dl_last) {
            g_dl_dupes++;
          } else if (delta < 0x80000000) {
            g_dl_gaps += delta;
          }
        }
        g_dl_last = seq;
        g_dl_have_last = 1;
        g_dl_recv++;
        struct timespec tnow;
        clock_gettime(CLOCK_MONOTONIC, &tnow);
        if (g_dl_ia_have) {
          double dt = (tnow.tv_sec - g_dl_ia_last.tv_sec) * 1000.0 + (tnow.tv_nsec - g_dl_ia_last.tv_nsec) / 1e6;
          g_dl_ia_sum += dt; g_dl_ia_sq += dt * dt; g_dl_ia_n++;
          if (dt > g_dl_ia_max) g_dl_ia_max = dt;
        }
        g_dl_ia_last = tnow;
        g_dl_ia_have = 1;
        ring_push((const int16_t *)payload, n);
      }
    } else if (type == T_FLUSH || type == T_RESET) {
      ring_flush();
      g_playing = 0;
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
      if (errno == EAGAIN || errno == EWOULDBLOCK) {
        pthread_mutex_unlock(&g_tx);
        return off == 0 ? 1 : -1;
      }
      pthread_mutex_unlock(&g_tx);
      return -1;
    }
    off += (size_t)n;
  }
  pthread_mutex_unlock(&g_tx);
  return 0;
}

static int send_uplink(uint64_t capture_us, const int16_t *pcm, int n) {
  unsigned char frame[10 + 8 + AEC_FRAME * 2];
  uint16_t magic = MAGIC;
  uint16_t len = (uint16_t)(8 + n * 2);
  frame[0] = magic & 0xff; frame[1] = magic >> 8;
  frame[2] = T_UPLINK; frame[3] = 0;
  uint32_t seq = (uint32_t)g_frames_sent;
  g_frames_sent++;
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
  unsigned char frame[10 + 48];
  uint16_t magic = MAGIC;
  frame[0] = magic & 0xff; frame[1] = magic >> 8;
  frame[2] = T_METER; frame[3] = 0;
  frame[4] = 0; frame[5] = 0; frame[6] = 0; frame[7] = 0;
  frame[8] = 48; frame[9] = 0;
  int32_t erle = (g_sum_out > 0.0) ? (int32_t)(10.0 * log10((g_sum_in + 1e-9) / (g_sum_out + 1e-9)) * 1000.0) : 0;
  uint32_t v[12];
  v[0] = (uint32_t)g_cap_errs;
  v[1] = (uint32_t)g_frames_sent;
  v[2] = (uint32_t)erle;
  v[3] = (uint32_t)g_dl_recv;
  v[4] = (uint32_t)g_dl_gaps;
  v[5] = (uint32_t)g_dl_dupes;
  v[6] = (uint32_t)g_underruns;
  v[7] = (uint32_t)g_dropped_frames;
  v[8] = (uint32_t)g_q_depth;
  v[9] = (uint32_t)g_q_max;
  v[10] = (uint32_t)g_ref_rms;
  v[11] = (uint32_t)g_out_rms;
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
  int sndbuf = 1 << 20;
  setsockopt(s, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof sndbuf);
  int rcvbuf = 1 << 20;
  setsockopt(s, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf);
  int one = 1;
  setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
  if (connect(s, res->ai_addr, res->ai_addrlen) != 0) {
    close(s);
    freeaddrinfo(res);
    return -1;
  }
  freeaddrinfo(res);
  return s;
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
  free(g_ring);
  g_ring = NULL;
  start_mixer();
}

int main(int argc, char **argv) {
  if (argc < 4) {
    fprintf(stderr, "usage: echo_live <server_ip> <server_port> <seconds|0>\n");
    return 2;
  }
  snprintf(g_server, sizeof g_server, "%s", argv[1]);
  g_port = atoi(argv[2]);
  double secs = atof(argv[3]);

  signal(SIGINT, on_sig);
  signal(SIGTERM, on_sig);
  signal(SIGPIPE, SIG_IGN);

  g_ring = malloc((size_t)g_ring_cap * 2);
  if (!g_ring) return 1;

  if (!getprop_equals("init.svc.mixer", "running")) {
    fprintf(stderr, "preflight: mixer not running; aborting\n");
    free(g_ring);
    return 3;
  }
  if (!d24_owner_is_mixer_only()) {
    fprintf(stderr, "preflight: D24 not owned solely by mixer; aborting\n");
    free(g_ring);
    return 4;
  }

  system("stop mixer");
  g_mixer_touched = 1;
  if (!wait_d24_closed(5000)) {
    fprintf(stderr, "D24 did not close after stop mixer; restoring\n");
    start_mixer();
    return 5;
  }

  struct pcm_config cc;
  memset(&cc, 0, sizeof cc);
  cc.channels = CHANS;
  cc.rate = 16000;
  cc.period_size = CAP_PERIOD;
  cc.period_count = CAP_COUNT;
  cc.format = 4;
  g_cap = pcm_open(0, 24, PCM_IN, &cc);
  if (!g_cap || !pcm_is_ready(g_cap)) {
    fprintf(stderr, "D24 open failed: %s\n", g_cap ? pcm_get_error(g_cap) : "null");
    cleanup();
    return 6;
  }

  struct pcm_config pc;
  memset(&pc, 0, sizeof pc);
  pc.channels = 2;
  pc.rate = DL_SR;
  pc.period_size = DL_CHUNK;
  pc.period_count = 4;
  pc.format = 0;
  g_pb = pcm_open(0, 23, PCM_OUT, &pc);
  if (!g_pb || !pcm_is_ready(g_pb)) {
    fprintf(stderr, "D23 open failed: %s\n", g_pb ? pcm_get_error(g_pb) : "null");
    cleanup();
    return 7;
  }
  g_alsa_buf_frames = (int)pcm_get_buffer_size(g_pb);
  g_prebuf = 2880;
  if (g_prebuf > g_ring_cap / 2) g_prebuf = g_ring_cap / 2;
  {
    int16_t sil[DL_CHUNK * 2];
    memset(sil, 0, sizeof sil);
    struct timespec pa, pb;
    clock_gettime(CLOCK_MONOTONIC, &pa);
    for (int k = 0; k < 4; k++) {
      int rc = pcm_write(g_pb, sil, sizeof sil);
      if (rc != 0) { pcm_prepare(g_pb); }
    }
    clock_gettime(CLOCK_MONOTONIC, &pb);
    fprintf(stderr, "D23 primed with 4 silence chunks in %.1f ms\n",
            (pb.tv_sec - pa.tv_sec) * 1000.0 + (pb.tv_nsec - pa.tv_nsec) / 1e6);
  }
  {
    char buf[512] = {0};
    FILE *hf = fopen("/proc/asound/card0/pcm23p/sub0/hw_params", "r");
    if (hf) {
      size_t n = fread(buf, 1, sizeof(buf) - 1, hf);
      fclose(hf);
      if (n) {
        for (size_t i = 0; i < n; i++) if (buf[i] == '\n') buf[i] = ' ';
      }
    }
    fprintf(stderr, "D23 opened: req period=%u count=%u alsa_buf_frames=%d prebuf=%d (%.1fms) | hw: %s\n",
            pc.period_size, pc.period_count, g_alsa_buf_frames, g_prebuf, g_prebuf * 1000.0 / DL_SR, buf);
  }

  g_st = speex_echo_state_init(AEC_FRAME, AEC_TAIL);
  int32_t rate = 16000;
  speex_echo_ctl(g_st, SPEEX_ECHO_SET_SAMPLING_RATE, &rate);

  g_sock = -1;
  for (int attempt = 0; attempt < 10 && g_sock < 0 && !g_stop; attempt++) {
    g_sock = connect_server(g_server, g_port);
    if (g_sock < 0) {
      fprintf(stderr, "connect %s:%d failed (%s), retry %d/10\n", g_server, g_port, strerror(errno), attempt + 1);
      usleep(500 * 1000);
    }
  }
  if (g_sock < 0) { cleanup(); return 8; }
  fprintf(stderr, "connected %s:%d frame=%d tail=%d mic_ch=%d ref_ch=%d dl=%dHz\n",
          g_server, g_port, AEC_FRAME, AEC_TAIL, MIC_CH, REF_CH, DL_SR);

  pthread_create(&g_play_th, NULL, playback_thread, NULL);
  g_play_started = 1;
  pthread_create(&g_rx_th, NULL, recv_thread, NULL);
  g_rx_started = 1;

  size_t per_bytes = (size_t)cc.period_size * cc.period_count * CHANS * 3;
  int block_frames = (int)(cc.period_size * cc.period_count);
  unsigned char *pr = malloc(per_bytes);
  int16_t *mic_all = malloc((size_t)(block_frames + AEC_FRAME) * 2);
  int16_t *ref_all = malloc((size_t)(block_frames + AEC_FRAME) * 2);
  int16_t mic[AEC_FRAME], ref[AEC_FRAME], out[AEC_FRAME];
  int carry = 0;
  struct timespec t0;
  clock_gettime(CLOCK_MONOTONIC, &t0);
  double last_meter = 0.0, win_ref = 0.0, win_out = 0.0;
  unsigned long win_n = 0;

  while (!g_stop) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    double el = (t.tv_sec - t0.tv_sec) + (t.tv_nsec - t0.tv_nsec) / 1e9;
    if (secs > 0 && el >= secs) break;

    if (pcm_read(g_cap, pr, per_bytes) != 0) {
      g_cap_errs++;
      if (g_cap_errs > 50) break;
      continue;
    }
    uint64_t cap_us;
    now_mono_us(&cap_us);

    int idx = carry;
    for (int i = 0; i < block_frames; i++) {
      unsigned char *m = pr + (size_t)i * CHANS * 3;
      unsigned char *r = pr + (size_t)i * CHANS * 3 + REF_CH * 3;
      int32_t vm = m[0] | (m[1] << 8) | (m[2] << 16);
      int32_t vr = r[0] | (r[1] << 8) | (r[2] << 16);
      if (vm & 0x800000) vm |= ~0xFFFFFF;
      if (vr & 0x800000) vr |= ~0xFFFFFF;
      mic_all[idx + i] = (int16_t)(vm >> 8);
      ref_all[idx + i] = (int16_t)(vr >> 8);
    }
    int total = idx + block_frames;
    int nfr = total / AEC_FRAME;
    for (int f = 0; f < nfr; f++) {
      memcpy(mic, mic_all + f * AEC_FRAME, AEC_FRAME * 2);
      memcpy(ref, ref_all + f * AEC_FRAME, AEC_FRAME * 2);
      speex_echo_cancellation(g_st, mic, ref, out);
      for (int i = 0; i < AEC_FRAME; i++) {
        double a = mic[i], b = out[i], c = ref[i];
        g_sum_in += a * a;
        g_sum_out += b * b;
        win_ref += c * c;
        win_out += b * b;
      }
      win_n += AEC_FRAME;
      if (send_uplink(cap_us, out, AEC_FRAME) != 0) {
        fprintf(stderr, "uplink send failed: %s\n", strerror(errno));
        g_stop = 1;
        break;
      }
    }
    int consumed = nfr * AEC_FRAME;
    carry = total - consumed;
    if (carry > 0) {
      memmove(mic_all, mic_all + consumed, (size_t)carry * 2);
      memmove(ref_all, ref_all + consumed, (size_t)carry * 2);
    }

    if (el - last_meter >= 1.0) {
      last_meter = el;
      g_ref_rms = win_n ? (int)sqrt(win_ref / win_n) : 0;
      g_out_rms = win_n ? (int)sqrt(win_out / win_n) : 0;
      win_ref = win_out = 0.0;
      win_n = 0;
      send_meter();
      double erle = g_sum_out > 0.0 ? 10.0 * log10((g_sum_in + 1e-9) / (g_sum_out + 1e-9)) : 0.0;
      fprintf(stderr, "t=%.1fs up=%llu drops=%llu errs=%d erle=%.2f ref_rms=%d out_rms=%d | dl=%llu gaps=%llu dupes=%llu underrun=%llu dropped_fr=%llu q=%d qmax=%d pberr=%llu pbw=%llu pbavg=%.2fms trims=%llu trimfr=%llu | dlia_mean=%.2fms std=%.2f max=%.1fms\n",
              el, g_frames_sent, g_tx_drops, g_cap_errs, erle, g_ref_rms, g_out_rms,
              g_dl_recv, g_dl_gaps, g_dl_dupes, g_underruns, g_dropped_frames, g_q_depth, g_q_max, g_pb_errs, g_pb_writes, g_pb_writes ? (g_pb_ns / 1e6 / g_pb_writes) : 0.0, g_trims, g_trim_frames,
              g_dl_ia_n ? g_dl_ia_sum / g_dl_ia_n : 0.0,
              g_dl_ia_n ? sqrt(g_dl_ia_sq / g_dl_ia_n - (g_dl_ia_sum / g_dl_ia_n) * (g_dl_ia_sum / g_dl_ia_n)) : 0.0,
              g_dl_ia_max);
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
  struct timespec te;
  clock_gettime(CLOCK_MONOTONIC, &te);
  double wall = (te.tv_sec - t0.tv_sec) + (te.tv_nsec - t0.tv_nsec) / 1e9;
  fprintf(stderr, "DONE up=%llu drops=%llu errs=%d erle=%.2f | dl=%llu gaps=%llu dupes=%llu underrun=%llu dropped_fr=%llu qmax=%d | cpu=%.2fs/%.1fs (%.1f%%) maxrss=%ldKB\n",
          g_frames_sent, g_tx_drops, g_cap_errs, erle,
          g_dl_recv, g_dl_gaps, g_dl_dupes, g_underruns, g_dropped_frames, g_q_max,
          cpu, wall, wall > 0 ? cpu / wall * 100.0 : 0.0, ru.ru_maxrss);
  free(pr);
  free(mic_all);
  free(ref_all);
  return 0;
}
