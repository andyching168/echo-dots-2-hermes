// Phase 5D.5B — independent LED ownership watchdog.
//
// Its only job: if ownership == HIYO (ledcontroller stopped) and the Hiyo
// renderer heartbeat goes stale, restore STOCK by starting ledcontroller.
// It never renders LEDs itself and does not depend on the renderer or the
// server, so it survives renderer crash / SIGKILL / OOM / server disconnect.
//
// Build:
//   $CC -O2 -fPIE -pie -o echo_led_watchdog echo_led_watchdog.c
// Run:
//   echo_led_watchdog [timeout_ms]

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <time.h>
#include <sys/stat.h>

#define LED_OWNER_FILE "/data/local/tmp/echo_led_owner"
#define LED_HB_FILE "/data/local/tmp/echo_led_hb"

static volatile sig_atomic_t g_stop = 0;
static void on_signal(int sig) { (void)sig; g_stop = 1; }

static int owner_is_hiyo(void) {
    FILE *f = fopen(LED_OWNER_FILE, "r");
    if (!f) return 0;
    int c = fgetc(f);
    fclose(f);
    return (c == 'H' || c == 'h') ? 1 : 0;
}

static void write_owner_stock(void) {
    FILE *f = fopen(LED_OWNER_FILE, "w");
    if (f) { fputs("STOCK\n", f); fclose(f); }
}

static double hb_age_ms(void) {
    struct stat st;
    if (stat(LED_HB_FILE, &st) != 0) return -1.0;
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    double age = (now.tv_sec - st.st_mtim.tv_sec) * 1000.0
               + (now.tv_nsec - st.st_mtim.tv_nsec) / 1e6;
    return age;
}

static int service_running(void) {
    FILE *p = popen("/system/bin/pidof ledcontroller 2>/dev/null", "r");
    if (!p) return 0;
    int pid = 0, n = fscanf(p, "%d", &pid);
    pclose(p);
    return (n == 1 && pid > 0) ? 1 : 0;
}

static void restore_stock(const char *reason) {
    if (!service_running()) {
        system("/system/bin/start ledcontroller");
        for (int i = 0; i < 40; i++) {
            if (service_running()) break;
            usleep(50 * 1000);
        }
    }
    write_owner_stock();
    fprintf(stderr, "[led-owner] watchdog restore reason=%s ledcontroller=%s\n",
            reason, service_running() ? "running" : "NOT-RUNNING");
    fflush(stderr);
}

int main(int argc, char **argv) {
    long timeout_ms = (argc > 1) ? atol(argv[1]) : 1500;
    if (timeout_ms < 300) timeout_ms = 300;

    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    signal(SIGPIPE, SIG_IGN);

    fprintf(stderr, "[led-owner] watchdog start timeout=%ldms\n", timeout_ms);
    fflush(stderr);

    while (!g_stop) {
        if (owner_is_hiyo()) {
            double age = hb_age_ms();
            if (age < 0 || age > (double)timeout_ms) {
                restore_stock("heartbeat_timeout");
                /* keep looping: harmless if still HIYO later */
            }
        }
        usleep(200 * 1000);
    }
    fprintf(stderr, "[led-owner] watchdog stop\n");
    return 0;
}
