#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define RESULT_DIR \
    "/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/"
#define JAVA_MODULE_SIZE 0x28
#define GLOBAL_MODULE_OFFSET 0xB0568ULL

typedef void *(*cj_init_fn)(const void *options);
typedef int64_t (*cj_create_sub_fn)(void *module, const char *sql, bool coord);
typedef int64_t (*cj_modify_sub_fn)(void *module, int64_t sub_id, const char *sql);
typedef int64_t (*cj_pause_sub_fn)(void *module, int64_t sub_id);
typedef void (*cj_register_cb_fn)(void *module, void *callback);
typedef int64_t (*cj_terminate_sub_fn)(void *module, int64_t sub_id);

static unsigned char g_module[JAVA_MODULE_SIZE];
static int64_t g_sub_id = -1;
static uint64_t g_callback_count = 0;
static char g_windcode[32] = "";
static char g_safe_code[32] = "";
static cj_modify_sub_fn g_modify_sub = NULL;
static cj_pause_sub_fn g_pause_sub = NULL;
static cj_terminate_sub_fn g_terminate_sub = NULL;

static void write_all(int fd, const void *data, size_t size) {
    const unsigned char *p = (const unsigned char *)data;
    while (size > 0) {
        ssize_t n = write(fd, p, size);
        if (n < 0) {
            if (errno == EINTR) continue;
            return;
        }
        p += (size_t)n;
        size -= (size_t)n;
    }
}

static void write_text(int fd, const char *text) {
    write_all(fd, text, strlen(text));
}

static void write_json_string(int fd, const char *text) {
    write_text(fd, "\"");
    if (text) {
        for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
            char buf[8];
            switch (*p) {
                case '\\': write_text(fd, "\\\\"); break;
                case '"': write_text(fd, "\\\""); break;
                case '\n': write_text(fd, "\\n"); break;
                case '\r': write_text(fd, "\\r"); break;
                case '\t': write_text(fd, "\\t"); break;
                default:
                    if (*p < 0x20) {
                        int n = snprintf(buf, sizeof(buf), "\\u%04x", *p);
                        write_all(fd, buf, (size_t)n);
                    } else {
                        write_all(fd, p, 1);
                    }
            }
        }
    }
    write_text(fd, "\"");
}

static void write_hex(int fd, const void *data, size_t size) {
    static const char digits[] = "0123456789abcdef";
    const unsigned char *p = (const unsigned char *)data;
    char buffer[4096];
    size_t used = 0;
    for (size_t i = 0; i < size; ++i) {
        buffer[used++] = digits[p[i] >> 4];
        buffer[used++] = digits[p[i] & 0x0f];
        if (used == sizeof(buffer)) {
            write_all(fd, buffer, used);
            used = 0;
        }
    }
    if (used) write_all(fd, buffer, used);
}

static uint32_t load_u32(const unsigned char *p) {
    uint32_t value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static uintptr_t load_ptr(const unsigned char *p) {
    uintptr_t value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static long long epoch_milliseconds(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_REALTIME, &ts) != 0) return 0;
    return (long long)ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;
}

static bool valid_windcode(const char *code) {
    if (!code || strlen(code) != 9) return false;
    for (int i = 0; i < 6; ++i) {
        if (code[i] < '0' || code[i] > '9') return false;
    }
    if (code[6] != '.') return false;
    return (code[7] == 'S' && (code[8] == 'Z' || code[8] == 'H'));
}

static void set_code(const char *code) {
    snprintf(g_windcode, sizeof(g_windcode), "%s", code);
    snprintf(g_safe_code, sizeof(g_safe_code), "%s", code);
    for (char *p = g_safe_code; *p; ++p) {
        if (*p == '.') *p = '_';
    }
}

static void write_status(const char *status, int64_t code, const char *message) {
    if (!g_safe_code[0]) return;
    char final_path[512];
    char temp_path[512];
    snprintf(final_path, sizeof(final_path), RESULT_DIR
             "wind_tbapi_live_%s_status.json", g_safe_code);
    snprintf(temp_path, sizeof(temp_path), "%s.%d.tmp", final_path, getpid());
    int fd = open(temp_path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;
    char head[512];
    int n = snprintf(head, sizeof(head),
        "{\"status\":\"%s\",\"code\":%lld,\"windcode\":\"%s\","
        "\"pid\":%d,\"epoch_ms\":%lld,\"message\":",
        status, (long long)code, g_windcode, getpid(), epoch_milliseconds());
    write_all(fd, head, (size_t)n);
    write_json_string(fd, message ? message : "");
    write_text(fd, "}\n");
    close(fd);
    (void)rename(temp_path, final_path);
}

static void subscription_callback(uint32_t sub_id, int32_t error_code,
                                  const char *error_message,
                                  const void *frame_ptr) {
    g_callback_count++;
    if (!g_safe_code[0]) return;

    char final_path[512];
    char temp_path[512];
    snprintf(final_path, sizeof(final_path), RESULT_DIR
             "wind_tbapi_live_%s.json", g_safe_code);
    snprintf(temp_path, sizeof(temp_path), "%s.%d.tmp", final_path, getpid());
    int fd = open(temp_path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;

    char head[768];
    int n = snprintf(head, sizeof(head),
        "{\n  \"status\":\"subscription_push\",\n"
        "  \"callback_seq\":%llu,\n  \"sub_id\":%u,\n"
        "  \"source_pid\":%d,\n  \"callback_epoch_ms\":%lld,\n"
        "  \"requested_windcode\":\"%s\",\n"
        "  \"error_code\":%d,\n  \"error_message\":",
        (unsigned long long)g_callback_count, sub_id, getpid(),
        epoch_milliseconds(), g_windcode, error_code);
    write_all(fd, head, (size_t)n);
    write_json_string(fd, error_message ? error_message : "");

    if (!frame_ptr) {
        write_text(fd, ",\n  \"frame\":null\n}\n");
        close(fd);
        (void)rename(temp_path, final_path);
        return;
    }

    const unsigned char *frame = (const unsigned char *)frame_ptr;
    uint32_t field_info_size = load_u32(frame + 0x2c);
    uint32_t buffer_58_size = load_u32(frame + 0x48);
    uint32_t buffer_60_size = load_u32(frame + 0x4c);
    uint32_t buffer_68_size = load_u32(frame + 0x50);
    uintptr_t field_info = load_ptr(frame + 0x40);
    uintptr_t buffer_58 = load_ptr(frame + 0x58);
    uintptr_t buffer_60 = load_ptr(frame + 0x60);
    uintptr_t buffer_68 = load_ptr(frame + 0x68);

    write_text(fd, ",\n  \"frame_header_hex\":\"");
    write_hex(fd, frame, 0x70);
    char fields[384];
    n = snprintf(fields, sizeof(fields),
        "\",\n  \"field_count\":%u,\n  \"field_info_size\":%u,\n"
        "  \"buffer_48_size\":%u,\n  \"buffer_4c_size\":%u,\n"
        "  \"buffer_50_size\":%u,\n",
        load_u32(frame + 0x28), field_info_size, buffer_58_size,
        buffer_60_size, buffer_68_size);
    write_all(fd, fields, (size_t)n);

#define WRITE_BUFFER(KEY, POINTER, SIZE) do { \
    write_text(fd, "  \"" KEY "\":{\"size\":"); \
    char size_text[32]; \
    int size_n = snprintf(size_text, sizeof(size_text), "%u", (SIZE)); \
    write_all(fd, size_text, (size_t)size_n); \
    write_text(fd, ",\"hex\":\""); \
    if ((POINTER) && (SIZE) <= 16u * 1024u * 1024u) \
        write_hex(fd, (const void *)(POINTER), (SIZE)); \
    write_text(fd, "\"}"); \
} while (0)

    WRITE_BUFFER("field_info", field_info, field_info_size);
    write_text(fd, ",\n");
    WRITE_BUFFER("buffer_58", buffer_58, buffer_58_size);
    write_text(fd, ",\n");
    WRITE_BUFFER("buffer_60", buffer_60, buffer_60_size);
    write_text(fd, ",\n");
    WRITE_BUFFER("buffer_68", buffer_68, buffer_68_size);
    write_text(fd, "\n}\n");
    close(fd);
    (void)rename(temp_path, final_path);
#undef WRITE_BUFFER
}

static int initialise_module(void) {
    const char *path = "/Applications/WindPersonFree.app/Contents/Frameworks/"
                       "libWind.Cosmos.TBAPI2.dylib";
    void *tbapi = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!tbapi) return 101;

    cj_init_fn init = (cj_init_fn)dlsym(tbapi, "CJAVAInit");
    cj_register_cb_fn register_cb = (cj_register_cb_fn)
        dlsym(tbapi, "CJAVARegisterQueryCallBack");
    g_modify_sub = (cj_modify_sub_fn)
        dlsym(tbapi, "CJAVAModifySubscription");
    g_pause_sub = (cj_pause_sub_fn)
        dlsym(tbapi, "CJAVAPauseSubscription");
    g_terminate_sub = (cj_terminate_sub_fn)
        dlsym(tbapi, "CJAVATerminateSubscription");
    if (!init || !register_cb || !g_modify_sub || !g_pause_sub || !g_terminate_sub) return 102;

    void *wind_module = NULL;
    Dl_info info;
    if (dladdr((const void *)init, &info) && info.dli_fbase) {
        uintptr_t *global_ptr = (uintptr_t *)
            ((uintptr_t)info.dli_fbase + GLOBAL_MODULE_OFFSET);
        wind_module = (void *)*global_ptr;
    }
    if (!wind_module) {
        unsigned char options[0x40];
        memset(options, 0, sizeof(options));
        wind_module = init(options);
    }
    if (!wind_module) return 103;

    memcpy(g_module, wind_module, JAVA_MODULE_SIZE);
    register_cb(g_module, (void *)subscription_callback);
    *(void **)(g_module + 0x00) = (void *)subscription_callback;
    return 0;
}

/*
 * wind_tbapi_subscribe — mimic Windʼs own ETF subscription lifecycle exactly.
 *
 * Wind native pattern (from TBAPI2 debug logs):
 *   1. CreateSubscription with WHERE windcode = ''
 *   2. ModifySubscription to target code
 *   3. When switching: PauseSubscription → ModifySubscription to new code
 *   4. When closing page:  PauseSubscription (NOT TerminateSubscription)
 *
 * SQL matches Windʼs F5ET.js exactly:
 *   6 fields, ETFComprehensive.WholeETFData, LATENCY(500 MS)
 */
__attribute__((visibility("default")))
int64_t wind_tbapi_subscribe(const char *windcode, int latency_ms) {
    if (!valid_windcode(windcode)) return -20001;
    (void)latency_ms;  /* we always use 500 MS — same as native Wind */
    set_code(windcode);

    /* ---- cold-start guard: give Wind time to finish TBAPI2 bootstrap ---- */
    {
        static bool _first_call = true;
        if (_first_call) {
            _first_call = false;
            usleep(3000000);  /* 3 s */
        }
    }

    /* ---- one-time module init ---- */
    int init_result = initialise_module();
    if (init_result != 0) {
        write_status("initialise_failed", init_result,
                     "TBAPI2 module unavailable");
        return -init_result;
    }

    g_callback_count = 0;

    /* ---- already subscribed? → pause then modify (Wind-natural switch) ---- */
    if (g_sub_id >= 0 && g_modify_sub && g_pause_sub) {
        (void)g_pause_sub(g_module, g_sub_id);
        char modify_sql[512];
        int n = snprintf(modify_sql, sizeof(modify_sql),
            "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
            "etfsellnumber, etfsellamount, etfsellmoney "
            "FROM ETFComprehensive.WholeETFData "
            "WHERE windcode = '%s' LATENCY(500 MS)", windcode);
        if (n <= 0 || (size_t)n >= sizeof(modify_sql)) return -20003;
        int64_t mod_result = g_modify_sub(g_module, g_sub_id, modify_sql);
        write_status(mod_result >= 0 ? "modify_switch" : "modify_failed",
                     mod_result, modify_sql);
        return mod_result;
    }

    /* ---- first subscription: create with empty windcode (mimics Wind UI) ---- */
    {
        void *tbapi = dlopen(
            "/Applications/WindPersonFree.app/Contents/Frameworks/"
            "libWind.Cosmos.TBAPI2.dylib", RTLD_NOW | RTLD_LOCAL);
        cj_create_sub_fn create_sub = (cj_create_sub_fn)
            dlsym(tbapi, "CJAVACreateSubscription");
        if (!create_sub) return -102;

        const char *create_sql =
            "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
            "etfsellnumber, etfsellamount, etfsellmoney "
            "FROM ETFComprehensive.WholeETFData "
            "WHERE windcode = '' LATENCY(500 MS)";

        g_sub_id = create_sub(g_module, create_sql, false);
        write_status(g_sub_id >= 0 ? "create_empty" : "create_failed",
                     g_sub_id, create_sql);
        if (g_sub_id < 0) return g_sub_id;
    }

    /* ---- modify subscription to target windcode ---- */
    {
        char modify_sql[512];
        int n = snprintf(modify_sql, sizeof(modify_sql),
            "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
            "etfsellnumber, etfsellamount, etfsellmoney "
            "FROM ETFComprehensive.WholeETFData "
            "WHERE windcode = '%s' LATENCY(500 MS)", windcode);
        if (n <= 0 || (size_t)n >= sizeof(modify_sql)) return -20003;
        int64_t mod_result = g_modify_sub(g_module, g_sub_id, modify_sql);
        write_status(mod_result >= 0 ? "modify_target" : "modify_failed",
                     mod_result, modify_sql);
        return mod_result;
    }
}

/*
 * wind_tbapi_stop — terminate the subscription.
 *
 * NOTE: Wind native uses PauseSubscription when the user navigates away
 * from the F5 page, so the subscription can be reused on return.  However,
 * our dylib instance is ephemeral (it cannot survive a Wind restart), so
 * pausing would leave orphan subscriptions on the server.  We terminate
 * explicitly to avoid accumulation.
 */
__attribute__((visibility("default")))
int64_t wind_tbapi_stop(void) {
    if (g_sub_id < 0) return 0;
    if (!g_terminate_sub) return -102;
    int64_t result = g_terminate_sub(g_module, g_sub_id);
    if (result >= 0) g_sub_id = -1;
    write_status("terminated", result, "subscription terminated");
    return result;
}

__attribute__((visibility("default")))
int64_t wind_tbapi_subscription_id(void) {
    return g_sub_id;
}
