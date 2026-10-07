// Phase 5B Echo physical-control daemon.
//
// Owns the Action + Mic-mute keys by EVIOCGRAB on /dev/input/event1, and
// *observes* the volume keys on /dev/input/event2 (left to stock acebuttond so
// volume is applied exactly once). Sends framed control messages to the
// echo_server control port and applies remote SET_VOLUME via the same LIPC
// implementation stock uses.
//
// The EVIOCGRAB is released automatically when the process exits (fd close),
// so a crash can never leave the buttons permanently inaccessible.
//
// Build:
//   $CC -O2 -fPIE -pie -o echo_control echo_control.c
// Run:
//   echo_control <host> <port> [nograb]

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <pthread.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <sys/poll.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <linux/input.h>

#define MAGIC 0xE0D1
#define T_CONTROL_HELLO 0x10
#define T_BUTTON_ACTION_DOWN 0x11
#define T_BUTTON_ACTION_UP 0x12
#define T_BUTTON_VOLUME_UP 0x13
#define T_BUTTON_VOLUME_DOWN 0x14
#define T_MIC_MUTE_STATE 0x15
#define T_VOLUME_VALUE 0x16
#define T_LED_OWNER_STATE 0x17   // Echo -> server: u8 owner(0=STOCK,1=HIYO) u8 mode
#define T_SET_VOLUME 0x90
#define T_LED_FRAME 0x91   // Phase 5D.5: 72-char IS31FL3236 frame from server
#define T_LED_OWN 0x92     // Phase 5D.5B: u8 acquire(1=HIYO,0=STOCK)
#define T_LED_MODE_CFG 0x93 // Phase 5D.5B: u8 mode(0=cooperative,1=exclusive)

#define EVDEV_ACTION "/dev/input/event1"
#define EVDEV_VOLUME "/dev/input/event2"
#define LED_FRAME_PATH "/sys/bus/i2c/devices/0-003f/frame"
#define LED_OWNER_FILE "/data/local/tmp/echo_led_owner"
#define LED_HB_FILE "/data/local/tmp/echo_led_hb"
#define LED_WATCHDOG "/data/local/tmp/echo_led_watchdog"
#define LED_SERVICE "ledcontroller"

#define LED_MODE_COOP 0
#define LED_MODE_EXCL 1
#define LED_OWN_STOCK 0
#define LED_OWN_HIYO 1

#define KEY_ACTION KEY_HELP  // 138
#define KEY_MUTEBTN KEY_MUTE // 113

static volatile sig_atomic_t g_stop = 0;
static int g_action_fd = -1;
static int g_volume_fd = -1;
static int g_sock = -1;
static unsigned int g_seq = 0;

static void on_signal(int sig) { (void)sig; g_stop = 1; }

static long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static int send_frame(int type, const unsigned char *payload, unsigned short len) {
    if (g_sock < 0) return -1;
    unsigned char hdr[10];
    hdr[0] = MAGIC & 0xff;
    hdr[1] = (MAGIC >> 8) & 0xff;
    hdr[2] = (unsigned char)type;
    hdr[3] = 0;
    unsigned int seq = g_seq++;
    hdr[4] = seq & 0xff; hdr[5] = (seq >> 8) & 0xff;
    hdr[6] = (seq >> 16) & 0xff; hdr[7] = (seq >> 24) & 0xff;
    hdr[8] = len & 0xff; hdr[9] = (len >> 8) & 0xff;
    struct iovec iov[2];
    iov[0].iov_base = hdr; iov[0].iov_len = 10;
    iov[1].iov_base = (void *)payload; iov[1].iov_len = len;
    struct msghdr msg; memset(&msg, 0, sizeof(msg));
    msg.msg_iov = iov; msg.msg_iovlen = (len ? 2 : 1);
    ssize_t n = sendmsg(g_sock, &msg, MSG_NOSIGNAL);
    return (n < 0) ? -1 : 0;
}

static int read_main_volume(void) {
    // Prefer the actual codec playback volume so the reported level matches what
    // is heard; fall back to the AVS (Puffin) property (0-100).
    FILE *p = popen("/system/bin/tinymix \"PCM Playback Volume\" 2>/dev/null", "r");
    if (p) {
        char line[160];
        if (fgets(line, sizeof(line), p)) {
            char *colon = strchr(line, ':');
            if (colon) {
                int pcm = atoi(colon + 1);
                pclose(p);
                if (pcm < 0) pcm = 0;
                if (pcm > 175) pcm = 175;
                return (pcm * 100 + 87) / 175;   // 0..175 -> 0..100
            }
        }
        pclose(p);
    }
    p = popen("/system/bin/lipc-get-prop -i -q com.amazon.puffin AvsVolume 2>/dev/null", "r");
    if (!p) return -1;
    int v = -1;
    if (fscanf(p, "%d", &v) != 1) v = -1;
    pclose(p);
    return v;
}

static int read_mute_state(void) {
    FILE *p = popen("/system/bin/lipc-get-prop -i -q com.doppler.buttond muteState 2>/dev/null", "r");
    if (!p) return -1;
    int v = -1;
    if (fscanf(p, "%d", &v) != 1) v = -1;
    pclose(p);
    return v;
}

static void set_mute_state(int muted) {
    char cmd[160];
    snprintf(cmd, sizeof(cmd),
             "/system/bin/lipc-set-prop -i com.doppler.buttond muteState %d", muted ? 9 : 8);
    if (system(cmd) != 0) { /* best effort */ }
    unsigned char b = muted ? 1 : 0;
    send_frame(T_MIC_MUTE_STATE, &b, 1);
}

// Map the 0-100 level onto the codec's PCM Playback Volume (0-175).  Linear is
// used deliberately: the stock speaker curve has plateaus (several UI steps share
// one output level), which would make some button presses silent.
static int vol_to_pcm(int v) {
    if (v < 0) v = 0;
    if (v > 100) v = 100;
    int pcm = (v * 175 + 50) / 100;     // 0..175
    if (pcm < 0) pcm = 0;
    if (pcm > 175) pcm = 175;
    return pcm;
}

static void apply_volume(int v) {
    if (v < 0) v = 0;
    if (v > 100) v = 100;
    char cmd[220];
    // Raw/endpoint mode bypasses the stock volume path, so drive the codec
    // directly (the mixer/audiod MainVolume source does not exist here).
    int pcm = vol_to_pcm(v);
    snprintf(cmd, sizeof(cmd),
             "/system/bin/tinymix \"PCM Playback Volume\" %d %d", pcm, pcm);
    if (system(cmd) != 0) { /* best effort */ }
    // Keep the stock properties in sync when the stock stack is up (best effort).
    snprintf(cmd, sizeof(cmd),
             "/system/bin/lipc-set-prop -i com.doppler.audiod MainVolume %d", v);
    if (system(cmd) != 0) { /* best effort */ }
    snprintf(cmd, sizeof(cmd),
             "/system/bin/lipc-set-prop -i com.amazon.puffin AvsVolume %d", v);
    if (system(cmd) != 0) { /* best effort */ }
}

// Phase 5D.5 / 5D.5B: sole owner of the IS31FL3236 frame node + ownership model.
//
// Two valid owners only:
//     STOCK = ledcontroller running, Hiyo renderer inactive
//     HIYO  = ledcontroller stopped, Hiyo renderer exclusively writes frame
// Default after boot is always STOCK.
//
// Arbitrator invariant: PHYSICAL MUTE RED > every Hiyo LED state.  This daemon
// reads the mute key itself, so it guarantees ordering: on mute it stops Hiyo
// writes and releases to STOCK *before* the stock red is needed; on unmute it
// only resumes when the server re-acquires, and never software-unmutes the mic.
//
// Mode:
//   cooperative - never stops ledcontroller; re-asserts frames fast to mask the
//                 occasional stock clobber (safe fallback / debug).
//   exclusive   - stops ledcontroller while a Hiyo state is visible; zero
//                 competing writers. Watchdog-protected.
static const char LED_ZERO[72 + 1] =
    "000000000000000000000000000000000000000000000000000000000000000000000000";
#define LED_SETTLE_MS 50

static pthread_mutex_t g_led_lock = PTHREAD_MUTEX_INITIALIZER;
static char g_led_frame[72];
static int g_led_have = 0;
static int g_led_dirty = 0;
static int g_led_suspended = 0;
static int g_led_owner = LED_OWN_STOCK;
static int g_led_mode = LED_MODE_COOP;
static int g_led_mute_hold = 0;
static volatile sig_atomic_t g_led_stop = 0;
static pthread_t g_led_thread;

// ---- stock service lifecycle ------------------------------------------------
static int led_service_running(void) {
    FILE *p = popen("/system/bin/pidof ledcontroller 2>/dev/null", "r");
    if (!p) return 0;
    int pid = 0;
    int n = fscanf(p, "%d", &pid);
    pclose(p);
    return (n == 1 && pid > 0) ? 1 : 0;
}

static int led_service_stop(void) {
    if (!led_service_running()) return 0;
    system("/system/bin/stop ledcontroller");
    for (int i = 0; i < 20; i++) {
        if (!led_service_running()) return 0;
        usleep(50 * 1000);
    }
    return -1;
}

static int led_service_start(void) {
    if (led_service_running()) return 0;
    system("/system/bin/start ledcontroller");
    for (int i = 0; i < 40; i++) {
        if (led_service_running()) return 0;
        usleep(50 * 1000);
    }
    return -1;
}

static void led_write_owner_file(void) {
    FILE *f = fopen(LED_OWNER_FILE, "w");
    if (f) { fputs(g_led_owner == LED_OWN_HIYO ? "HIYO\n" : "STOCK\n", f); fclose(f); }
}

static void led_set_owner(int owner, const char *reason) {
    pthread_mutex_lock(&g_led_lock);
    int old = g_led_owner;
    g_led_owner = owner;
    pthread_mutex_unlock(&g_led_lock);
    led_write_owner_file();
    if (old != owner) {
        fprintf(stderr, "[led-owner] %s -> %s reason=%s\n",
                old == LED_OWN_HIYO ? "HIYO" : "STOCK",
                owner == LED_OWN_HIYO ? "HIYO" : "STOCK", reason);
        unsigned char pl[2] = { (unsigned char)owner, (unsigned char)g_led_mode };
        send_frame(T_LED_OWNER_STATE, pl, 2);
    }
}

static void led_blank_now(void) {
    int fd = open(LED_FRAME_PATH, O_WRONLY);
    if (fd >= 0) {
        if (lseek(fd, 0, SEEK_SET) >= 0) { ssize_t n = write(fd, LED_ZERO, 72); (void)n; }
        close(fd);
    }
}

// HIYO acquisition (exclusive): stop stock, verify, settle, then render.
static int led_acquire(const char *reason) {
    if (g_led_mute_hold) {
        fprintf(stderr, "[led-owner] acquire refused reason=%s (physical mute)\n", reason);
        return -1;
    }
    if (g_led_mode == LED_MODE_COOP) {
        led_set_owner(LED_OWN_HIYO, reason);   // logical guest only; stock keeps running
        return 0;
    }
    if (led_service_running()) {
        if (led_service_stop() != 0) {
            fprintf(stderr, "[led-owner] acquire FAILED reason=%s (ledcontroller won't stop); staying STOCK\n",
                    reason);
            led_set_owner(LED_OWN_STOCK, reason);
            return -1;
        }
        fprintf(stderr, "[led-owner] ledcontroller stopped\n");
    }
    usleep(LED_SETTLE_MS * 1000);
    led_set_owner(LED_OWN_HIYO, reason);
    return 0;
}

// Release to STOCK: stop rendering, clear animation, restore ledcontroller.
static int led_release(const char *reason) {
    led_set_owner(LED_OWN_STOCK, reason);       // renderer stops (owner != HIYO)
    pthread_mutex_lock(&g_led_lock);
    g_led_have = 0;
    g_led_dirty = 0;
    pthread_mutex_unlock(&g_led_lock);
    led_blank_now();                            // clear animation before stock red
    if (g_led_mode == LED_MODE_EXCL) {
        if (led_service_start() == 0)
            fprintf(stderr, "[led-owner] ledcontroller started\n");
        else
            fprintf(stderr, "[led-owner] ledcontroller start FAILED reason=%s\n", reason);
    }
    return 0;
}

static int led_ensure_stock(const char *reason) {
    return led_release(reason);
}

static void led_set_suspended(int v) {
    pthread_mutex_lock(&g_led_lock);
    g_led_suspended = v ? 1 : 0;
    pthread_mutex_unlock(&g_led_lock);
}

// Physical mute: highest priority.
static void led_owner_on_mute(const char *reason) {
    g_led_mute_hold = 1;
    led_set_suspended(1);                       // stop Hiyo writes immediately
    if (g_led_mode == LED_MODE_EXCL) {
        led_release(reason);                    // -> start ledcontroller -> stock red
    } else if (!led_service_running()) {
        led_service_start();
    }
    fprintf(stderr, "[led-owner] release reason=%s (physical mute)\n", reason);
}

static void led_owner_on_unmute(const char *reason) {
    g_led_mute_hold = 0;
    led_set_suspended(0);
    // Exclusive: remain STOCK until the server explicitly re-acquires.
    led_set_owner(LED_OWN_STOCK, reason);
    fprintf(stderr, "[led-owner] unmute reason=%s (owner=STOCK, awaiting acquire)\n", reason);
}

static void led_set_frame(const unsigned char *hex, int len) {
    if (len != 72) return;
    pthread_mutex_lock(&g_led_lock);
    memcpy(g_led_frame, hex, 72);
    g_led_have = 1;
    g_led_dirty = 1;
    pthread_mutex_unlock(&g_led_lock);
}

static void *led_thread_main(void *arg) {
    (void)arg;
    int fd = -1;
    char buf[72];
    long last_hb = 0;
    while (!g_led_stop) {
        pthread_mutex_lock(&g_led_lock);
        int suspended = g_led_suspended;
        int have = g_led_have;
        int dirty = g_led_dirty;
        int mode = g_led_mode;
        int owner = g_led_owner;
        if (have) memcpy(buf, g_led_frame, 72);
        int allow = have && !suspended && (mode == LED_MODE_COOP || owner == LED_OWN_HIYO);
        if (allow && (mode == LED_MODE_COOP || dirty)) {
            if (fd < 0) fd = open(LED_FRAME_PATH, O_WRONLY);
            if (fd >= 0 && lseek(fd, 0, SEEK_SET) >= 0) {
                ssize_t n = write(fd, buf, 72);
                if (n != 72) { close(fd); fd = -1; }
            }
            if (dirty) g_led_dirty = 0;
        }
        pthread_mutex_unlock(&g_led_lock);

        if (mode == LED_MODE_EXCL && owner == LED_OWN_HIYO) {
            long t = now_ms();
            if (t - last_hb >= 200) {
                FILE *f = fopen(LED_HB_FILE, "w");
                if (f) { fputc('1', f); fclose(f); }
                last_hb = t;
            }
        }
        int active = 0;
        for (int i = 0; i < 72; i++) {
            if (have && buf[i] != '0') { active = 1; break; }
        }
        if (mode == LED_MODE_COOP)
            usleep(active ? 5 * 1000 : 100 * 1000);  // mask stock clobbers
        else
            usleep(10 * 1000);                        // exclusive: dirty-driven only
    }
    if (fd >= 0) close(fd);
    return NULL;
}

static void led_restore_off(void) {
    pthread_mutex_lock(&g_led_lock);
    if (g_led_suspended) {           // physically muted: never touch stock red
        pthread_mutex_unlock(&g_led_lock);
        return;
    }
    g_led_have = 0;
    g_led_dirty = 0;
    pthread_mutex_unlock(&g_led_lock);
    led_blank_now();
}

// Spawn the independent watchdog once (exclusive only). The watchdog survives
// this process and restores stock if our heartbeat goes stale.
static void led_ensure_watchdog(void) {
    if (g_led_mode != LED_MODE_EXCL) return;
    FILE *p = popen("/system/bin/pidof echo_led_watchdog 2>/dev/null", "r");
    int running = 0, pid = 0;
    if (p) { running = (fscanf(p, "%d", &pid) == 1 && pid > 0); pclose(p); }
    if (!running) {
        system("setsid " LED_WATCHDOG " 1500 >/data/local/tmp/echo_led_watchdog.log 2>&1 < /dev/null &");
        fprintf(stderr, "[led-owner] watchdog spawned\n");
    }
}

static int connect_server(const char *host, int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    struct sockaddr_in sa; memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons((unsigned short)port);
    if (inet_pton(AF_INET, host, &sa.sin_addr) != 1) { close(fd); return -1; }
    if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) { close(fd); return -1; }
    return fd;
}

// Drain server->Echo frames (SET_VOLUME / LED_MODE) without blocking.
static void service_socket(unsigned char *rbuf, int *rlen) {
    unsigned char tmp[512];
    ssize_t n = recv(g_sock, tmp, sizeof(tmp), MSG_DONTWAIT);
    if (n == 0) { close(g_sock); g_sock = -1; led_ensure_stock("control_loss"); return; }
    if (n < 0) {
        if (errno != EAGAIN && errno != EWOULDBLOCK) {
            close(g_sock); g_sock = -1; led_ensure_stock("control_loss");
        }
        return;
    }
    if (*rlen + (int)n > (int)sizeof(tmp) * 4) *rlen = 0;
    memcpy(rbuf + *rlen, tmp, n);
    *rlen += n;
    // parse complete frames
    int off = 0;
    while (*rlen - off >= 10) {
        unsigned char *p = rbuf + off;
        unsigned short magic = p[0] | (p[1] << 8);
        unsigned char type = p[2];
        unsigned short len = p[8] | (p[9] << 8);
        if (magic != MAGIC) { off = *rlen; break; }
        if (*rlen - off < 10 + len) break;
        if (type == T_SET_VOLUME && len >= 1) {
            apply_volume(p[10]);
        } else if (type == T_LED_FRAME && len >= 6 && len <= 256) {
            led_set_frame(p + 10, len);
        } else if (type == T_LED_OWN && len >= 1) {
            if (p[10]) led_acquire("server_acquire");
            else led_release("server_release");
        } else if (type == T_LED_MODE_CFG && len >= 1) {
            int m = p[10] ? LED_MODE_EXCL : LED_MODE_COOP;
            if (m != g_led_mode) {
                if (g_led_owner == LED_OWN_HIYO) led_release("mode_change");
                g_led_mode = m;
                fprintf(stderr, "[led-owner] mode=%s\n",
                        m == LED_MODE_EXCL ? "exclusive" : "cooperative");
                if (m == LED_MODE_EXCL) led_ensure_watchdog();
                unsigned char pl[2] = { (unsigned char)g_led_owner, (unsigned char)g_led_mode };
                send_frame(T_LED_OWNER_STATE, pl, 2);
            }
        }
        off += 10 + len;
    }
    if (off > 0) { memmove(rbuf, rbuf + off, *rlen - off); *rlen -= off; }
}

int main(int argc, char **argv) {
    const char *host = (argc > 1) ? argv[1] : "192.168.0.212";
    int port = (argc > 2) ? atoi(argv[2]) : 8793;
    int do_grab = !(argc > 3 && strcmp(argv[3], "nograb") == 0);

    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    signal(SIGPIPE, SIG_IGN);

    g_action_fd = open(EVDEV_ACTION, O_RDONLY);
    if (g_action_fd < 0) { perror("open event1"); return 1; }
    g_volume_fd = open(EVDEV_VOLUME, O_RDONLY);
    if (g_volume_fd < 0) { fprintf(stderr, "warn: open event2 failed: %s\n", strerror(errno)); }

    if (do_grab) {
        if (ioctl(g_action_fd, EVIOCGRAB, 1) < 0) {
            perror("EVIOCGRAB event1"); // continue anyway (observe-only)
        } else {
            fprintf(stderr, "[echo_control] EVIOCGRAB event1 ok\n");
        }
    }

    int muted = (read_mute_state() == 9);
    // Mode: argv[4] (cooperative|exclusive) or ECHO_LED_MODE env; default cooperative.
    const char *mode_s = (argc > 4) ? argv[4] : getenv("ECHO_LED_MODE");
    g_led_mode = (mode_s && strcmp(mode_s, "exclusive") == 0) ? LED_MODE_EXCL : LED_MODE_COOP;
    g_led_owner = LED_OWN_STOCK;
    led_write_owner_file();              // boot default is always STOCK
    g_led_mute_hold = muted ? 1 : 0;
    led_set_suspended(muted ? 1 : 0);   // never overwrite a pre-existing stock red
    if (g_led_mode == LED_MODE_EXCL) led_ensure_watchdog();
    if (pthread_create(&g_led_thread, NULL, led_thread_main, NULL) != 0)
        fprintf(stderr, "[echo_control] warn: LED renderer thread not started\n");
    fprintf(stderr, "[echo_control] start host=%s port=%d grab=%d muteState=%d led_mode=%s\n",
            host, port, do_grab, muted,
            g_led_mode == LED_MODE_EXCL ? "exclusive" : "cooperative");

    unsigned char rbuf[2048];
    int rlen = 0;
    long action_down_ms = 0;
    int action_is_down = 0;
    long last_wd_check = 0;

    while (!g_stop) {
        // Keep the independent watchdog alive (exclusive mode). If both this
        // daemon and the watchdog die, a reboot / `start ledcontroller`
        // restores stock; `stop` is never persistent.
        if (g_led_mode == LED_MODE_EXCL) {
            long t = now_ms();
            if (t - last_wd_check > 3000) { last_wd_check = t; led_ensure_watchdog(); }
        }
        if (g_sock < 0) {
            g_sock = connect_server(host, port);
            if (g_sock >= 0) {
                send_frame(T_CONTROL_HELLO, NULL, 0);
                unsigned char b = muted ? 1 : 0;
                send_frame(T_MIC_MUTE_STATE, &b, 1);
                int v = read_main_volume();
                if (v >= 0) { unsigned char vb = (unsigned char)v; send_frame(T_VOLUME_VALUE, &vb, 1); }
                rlen = 0;
                fprintf(stderr, "[echo_control] connected to %s:%d (vol=%d mute=%d)\n",
                        host, port, v, muted);
            } else {
                usleep(1000 * 1000);
                continue;
            }
        }

        struct pollfd pfd[3];
        int nf = 0;
        pfd[nf].fd = g_action_fd; pfd[nf].events = POLLIN; nf++;
        if (g_volume_fd >= 0) { pfd[nf].fd = g_volume_fd; pfd[nf].events = POLLIN; nf++; }
        pfd[nf].fd = g_sock; pfd[nf].events = POLLIN; nf++;

        int pr = poll(pfd, nf, 500);
        if (pr < 0) { if (errno == EINTR) continue; break; }

        for (int i = 0; i < nf; i++) {
            if (!(pfd[i].revents & POLLIN)) continue;
            if (pfd[i].fd == g_sock) {
                service_socket(rbuf, &rlen);
                continue;
            }
            struct input_event ev;
            ssize_t r = read(pfd[i].fd, &ev, sizeof(ev));
            if (r != (ssize_t)sizeof(ev)) continue;
            if (ev.type != EV_KEY) continue;
            if (pfd[i].fd == g_action_fd) {
                if (ev.code == KEY_ACTION) {
                    if (ev.value == 1) { action_is_down = 1; action_down_ms = now_ms();
                        send_frame(T_BUTTON_ACTION_DOWN, NULL, 0);
                        fprintf(stderr, "[echo_control] action DOWN\n");
                    } else if (ev.value == 0 && action_is_down) {
                        action_is_down = 0;
                        long hold = now_ms() - action_down_ms;
                        unsigned char pl[4] = { hold & 0xff, (hold >> 8) & 0xff,
                                                (hold >> 16) & 0xff, (hold >> 24) & 0xff };
                        send_frame(T_BUTTON_ACTION_UP, pl, 4);
                        fprintf(stderr, "[echo_control] action UP hold=%ldms\n", hold);
                    }
                } else if (ev.code == KEY_MUTEBTN && ev.value == 1) {
                    if (!muted) {
                        // MUTING: release to stock FIRST so the stock red
                        // indication can never be overwritten.
                        muted = 1;
                        led_owner_on_mute("physical_mute");
                        set_mute_state(muted);
                        fprintf(stderr, "[echo_control] mic mute -> 1\n");
                    } else {
                        // UNMUTING: clear stock red; do NOT auto-reacquire.
                        muted = 0;
                        set_mute_state(muted);
                        led_owner_on_unmute("physical_unmute");
                        fprintf(stderr, "[echo_control] mic mute -> 0\n");
                    }
                }
            } else if (pfd[i].fd == g_volume_fd) {
                // Volume is owned by the server in raw mode: report the press and
                // let the controller step the level, apply it (T_SET_VOLUME ->
                // tinymix) and drive the stock tone + ring feedback.  We do NOT
                // report a value here (AvsVolume is not updated in this mode and
                // would clobber the controller's level).
                if (ev.value == 1 && ev.code == KEY_VOLUMEUP) {
                    send_frame(T_BUTTON_VOLUME_UP, NULL, 0);
                } else if (ev.value == 1 && ev.code == KEY_VOLUMEDOWN) {
                    send_frame(T_BUTTON_VOLUME_DOWN, NULL, 0);
                }
            }
        }
    }

    if (do_grab && g_action_fd >= 0) {
        if (ioctl(g_action_fd, EVIOCGRAB, 0) == 0)
            fprintf(stderr, "[echo_control] EVIOCGRAB released\n");
    }
    // Clean shutdown: stop the renderer thread, then hand the ring back to stock
    // (release starts ledcontroller if we owned it) or restore off.
    g_led_stop = 1;
    pthread_join(g_led_thread, NULL);
    if (g_led_mode == LED_MODE_EXCL && g_led_owner == LED_OWN_HIYO && !g_led_mute_hold)
        led_release("shutdown");
    else
        led_restore_off();
    if (g_action_fd >= 0) close(g_action_fd);
    if (g_volume_fd >= 0) close(g_volume_fd);
    if (g_sock >= 0) close(g_sock);
    fprintf(stderr, "[echo_control] stopped\n");
    return 0;
}
