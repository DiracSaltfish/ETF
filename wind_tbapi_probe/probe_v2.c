#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/*
 * probe_v2.c — Wind TBAPI2 read-only subscription probe.
 *
 * Key improvement over probe.c:
 * - Calls CJAVAInit(NULL) to retrieve Windʼs existing, authenticated JavaAPIModule.
 * - Copies the module and replaces only the callback pointer at offset 0x20.
 * - Uses CreateSubscription → ModifySubscription (matching Windʼs own UI flow).
 * - Callback writes each data push to a separate numbered JSON file.
 *
 * This intentionally does NOT initialise a second session, read credentials,
 * or send trading instructions.
 */

/* ------------------------------------------------------------------ */
/* Paths                                                               */
/* ------------------------------------------------------------------ */

#define kResultDir \
    "/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/"

/* ------------------------------------------------------------------ */
/* Module layout (reverse-engineered from CJAVAInit disassembly)       */
/* ------------------------------------------------------------------ */

#define JAVA_MODULE_SIZE  0x28    /* 40 bytes                             */
#define CB_OFFSET         0x20    /* callback pointer stored at +0x20      */

/* ------------------------------------------------------------------ */
/* Function pointer typedefs                                           */
/* ------------------------------------------------------------------ */

typedef void *(*cj_init_fn)(const void *options);
typedef int64_t (*cj_create_sub_fn)(void *module, const char *sql, bool coord);
typedef int64_t (*cj_modify_sub_fn)(void *module, int64_t sub_id,
                                    const char *sql);
typedef void (*cj_register_cb_fn)(void *module, void *callback);
typedef int64_t (*cj_pause_sub_fn)(void *module, int64_t sub_id);
typedef int64_t (*cj_term_sub_fn)(void *module, int64_t sub_id);

/* ------------------------------------------------------------------ */
/* Small helpers                                                       */
/* ------------------------------------------------------------------ */

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

static void write_text(int fd, const char *s) {
    write_all(fd, s, strlen(s));
}

static void write_json_string(int fd, const char *s) {
    write_text(fd, "\"");
    if (s) {
        for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
            char buf[8];
            switch (*p) {
                case '\\': write_text(fd, "\\\\"); break;
                case '"':  write_text(fd, "\\\""); break;
                case '\n': write_text(fd, "\\n");  break;
                case '\r': write_text(fd, "\\r");  break;
                case '\t': write_text(fd, "\\t");  break;
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
    char buf[4096];
    size_t used = 0;
    for (size_t i = 0; i < size; ++i) {
        buf[used++] = digits[p[i] >> 4];
        buf[used++] = digits[p[i] & 0x0f];
        if (used == sizeof(buf)) {
            write_all(fd, buf, used);
            used = 0;
        }
    }
    if (used) write_all(fd, buf, used);
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

/* ------------------------------------------------------------------ */
/* Global state (must outlive the constructor)                         */
/* ------------------------------------------------------------------ */

static unsigned char g_our_module[JAVA_MODULE_SIZE];
static int g_callback_count = 0;
static int64_t g_sub_id = -1;

/* Function pointers cached for potential cleanup */
static cj_pause_sub_fn  g_pause_sub  = NULL;
static cj_term_sub_fn   g_term_sub   = NULL;

/* ------------------------------------------------------------------ */
/* Subscription callback — called by TBAPI2 on every 500-ms push       */
/* ------------------------------------------------------------------ */

static void sub_callback(int64_t sub_id, int32_t error_code,
                         const char *error_message, const void *frame_ptr) {
    g_callback_count++;

    char path[512];
    snprintf(path, sizeof(path), "%s/wind_tbapi_probe_sub_%d.json",
             kResultDir, g_callback_count);

    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;

    /* --- header --- */
    char head[384];
    int n = snprintf(head, sizeof(head),
        "{\n"
        "  \"status\": \"subscription_push\",\n"
        "  \"callback_seq\": %d,\n"
        "  \"sub_id\": %lld,\n"
        "  \"error_code\": %d,\n"
        "  \"error_message\": ",
        g_callback_count, (long long)sub_id, error_code);
    write_all(fd, head, (size_t)n);
    write_json_string(fd, error_message ? error_message : "");

    /* --- frame dump --- */
    if (!frame_ptr) {
        write_text(fd, ",\n  \"frame\": null\n}\n");
        close(fd);
        return;
    }

    const unsigned char *f = (const unsigned char *)frame_ptr;

    write_text(fd, ",\n  \"frame_header_hex\": \"");
    write_hex(fd, f, 0x70);
    write_text(fd, "\",");

    /* known offsets inside JavaTableFrame (same as probe.c) */
    char fields[320];
    n = snprintf(fields, sizeof(fields),
        "\n  \"field_count\": %u,\n"
        "  \"field_info_size\": %u,\n"
        "  \"value_30\": %llu,\n"
        "  \"value_38\": %u,\n"
        "  \"buffer_48_size\": %u,\n"
        "  \"buffer_4c_size\": %u,\n"
        "  \"buffer_50_size\": %u,\n",
        load_u32(f + 0x28), load_u32(f + 0x2c),
        (unsigned long long)load_ptr(f + 0x30), load_u32(f + 0x38),
        load_u32(f + 0x48), load_u32(f + 0x4c), load_u32(f + 0x50));
    write_all(fd, fields, (size_t)n);

    /* buffer dumps (field_info first, then the three data buffers) */
    write_text(fd, "  \"field_info\": {"
        "\"address\": \"0x");
    char addr[32];
    n = snprintf(addr, sizeof(addr), "%llx",
                 (unsigned long long)load_ptr(f + 0x40));
    write_all(fd, addr, (size_t)n);
    write_text(fd, "\", \"size\": ");
    char sz[32];
    n = snprintf(sz, sizeof(sz), "%u", load_u32(f + 0x2c));
    write_all(fd, sz, (size_t)n);
    write_text(fd, ", \"hex\": \"");
    uintptr_t fi_ptr = load_ptr(f + 0x40);
    uint32_t  fi_sz   = load_u32(f + 0x2c);
    if (fi_ptr && fi_sz && fi_sz <= (16u * 1024u * 1024u))
        write_hex(fd, (const void *)fi_ptr, fi_sz);
    write_text(fd, "\"}");

    write_text(fd, ",\n  \"buffer_58\": {"
        "\"address\": \"0x");
    n = snprintf(addr, sizeof(addr), "%llx",
                 (unsigned long long)load_ptr(f + 0x58));
    write_all(fd, addr, (size_t)n);
    write_text(fd, "\", \"size\": ");
    n = snprintf(sz, sizeof(sz), "%u", load_u32(f + 0x48));
    write_all(fd, sz, (size_t)n);
    write_text(fd, ", \"hex\": \"");
    uintptr_t b58 = load_ptr(f + 0x58);
    uint32_t  s48 = load_u32(f + 0x48);
    if (b58 && s48 && s48 <= (16u * 1024u * 1024u))
        write_hex(fd, (const void *)b58, s48);
    write_text(fd, "\"}");

    write_text(fd, ",\n  \"buffer_60\": {"
        "\"address\": \"0x");
    n = snprintf(addr, sizeof(addr), "%llx",
                 (unsigned long long)load_ptr(f + 0x60));
    write_all(fd, addr, (size_t)n);
    write_text(fd, "\", \"size\": ");
    n = snprintf(sz, sizeof(sz), "%u", load_u32(f + 0x4c));
    write_all(fd, sz, (size_t)n);
    write_text(fd, ", \"hex\": \"");
    uintptr_t b60 = load_ptr(f + 0x60);
    uint32_t  s4c = load_u32(f + 0x4c);
    if (b60 && s4c && s4c <= (16u * 1024u * 1024u))
        write_hex(fd, (const void *)b60, s4c);
    write_text(fd, "\"}");

    write_text(fd, ",\n  \"buffer_68\": {"
        "\"address\": \"0x");
    n = snprintf(addr, sizeof(addr), "%llx",
                 (unsigned long long)load_ptr(f + 0x68));
    write_all(fd, addr, (size_t)n);
    write_text(fd, "\", \"size\": ");
    n = snprintf(sz, sizeof(sz), "%u", load_u32(f + 0x50));
    write_all(fd, sz, (size_t)n);
    write_text(fd, ", \"hex\": \"");
    uintptr_t b68 = load_ptr(f + 0x68);
    uint32_t  s50 = load_u32(f + 0x50);
    if (b68 && s50 && s50 <= (16u * 1024u * 1024u))
        write_hex(fd, (const void *)b68, s50);
    write_text(fd, "\"}");

    write_text(fd, "\n}\n");
    close(fd);

    /* Only keep the first 10 callbacks active to avoid log spam */
    if (g_callback_count >= 10 && g_pause_sub && g_sub_id > 0) {
        g_pause_sub(g_our_module, g_sub_id);
    }
}

/* ------------------------------------------------------------------ */
/* Main probe entry point                                              */
/* ------------------------------------------------------------------ */

int wind_tbapi_probe_run(void) {
    const char *tbapi_path =
        "/Applications/WindPersonFree.app/Contents/Frameworks/"
        "libWind.Cosmos.TBAPI2.dylib";

    void *tbapi = dlopen(tbapi_path, RTLD_NOW | RTLD_LOCAL);
    if (!tbapi) {
        int fd = open(kResultDir "wind_tbapi_probe_v2_status.json",
                      O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (fd >= 0) {
            write_text(fd, "{\"status\": \"dlopen_failed\"}\n");
            close(fd);
        }
        return 101;
    }

    /* --- resolve symbols --- */
    cj_init_fn        CJAVAInit        = (cj_init_fn)
        dlsym(tbapi, "CJAVAInit");
    cj_create_sub_fn  CJAVACreateSub   = (cj_create_sub_fn)
        dlsym(tbapi, "CJAVACreateSubscription");
    cj_modify_sub_fn  CJAVAModifySub   = (cj_modify_sub_fn)
        dlsym(tbapi, "CJAVAModifySubscription");
    cj_register_cb_fn CJAVARegCB       = (cj_register_cb_fn)
        dlsym(tbapi, "CJAVARegisterQueryCallBack");
    g_pause_sub = (cj_pause_sub_fn)
        dlsym(tbapi, "CJAVAPauseSubscription");
    g_term_sub  = (cj_term_sub_fn)
        dlsym(tbapi, "CJAVATerminateSubscription");

    if (!CJAVAInit || !CJAVACreateSub || !CJAVAModifySub || !CJAVARegCB) {
        int fd = open(kResultDir "wind_tbapi_probe_v2_status.json",
                      O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (fd >= 0) {
            write_text(fd, "{\"status\": \"dlsym_failed\"}\n");
            close(fd);
        }
        return 102;
    }

    /* --- get Windʼs existing JavaAPIModule singleton --- */
    void *wind_module = CJAVAInit(NULL);
    if (!wind_module) {
        int fd = open(kResultDir "wind_tbapi_probe_v2_status.json",
                      O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (fd >= 0) {
            write_text(fd, "{\"status\": \"cj_init_returned_null\"}\n");
            close(fd);
        }
        return 103;
    }

    /* --- copy module and install OUR callback --- */
    memcpy(g_our_module, wind_module, JAVA_MODULE_SIZE);
    CJAVARegCB(g_our_module, (void *)sub_callback);

    /* --- status: got module --- */
    {
        int fd = open(kResultDir "wind_tbapi_probe_v2_status.json",
                      O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (fd >= 0) {
            char buf[256];
            int n = snprintf(buf, sizeof(buf),
                "{\"status\": \"got_wind_module\", "
                "\"wind_module\": \"0x%llx\", "
                "\"our_module\": \"0x%llx\"}\n",
                (unsigned long long)wind_module,
                (unsigned long long)g_our_module);
            write_all(fd, buf, (size_t)n);
            close(fd);
        }
    }

    /* --- Step 1: Create subscription (empty windcode, mimics Wind UI) --- */
    const char *create_sql =
        "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
        "etfsellnumber, etfsellamount, etfsellmoney "
        "FROM ETFComprehensive.WholeETFData "
        "WHERE windcode = ''";

    int64_t sub_id = CJAVACreateSub(g_our_module, create_sql, false);

    {
        int fd = open(kResultDir "wind_tbapi_probe_v2_status.json",
                      O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (fd >= 0) {
            char buf[256];
            int n = snprintf(buf, sizeof(buf),
                "{\"status\": \"create_sub_done\", \"sub_id\": %lld}\n",
                (long long)sub_id);
            write_all(fd, buf, (size_t)n);
            close(fd);
        }
    }

    if (sub_id < 0) return (int)sub_id;

    g_sub_id = sub_id;

    /* --- Step 2: Modify subscription to target windcode --- */
    const char *modify_sql =
        "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
        "etfsellnumber, etfsellamount, etfsellmoney "
        "FROM ETFComprehensive.WholeETFData "
        "WHERE windcode = '159518.SZ' LATENCY(500 MS)";

    int64_t mod_result = CJAVAModifySub(g_our_module, sub_id, modify_sql);

    {
        int fd = open(kResultDir "wind_tbapi_probe_v2_status.json",
                      O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (fd >= 0) {
            char buf[384];
            int n = snprintf(buf, sizeof(buf),
                "{\"status\": \"modify_sub_done\", "
                "\"sub_id\": %lld, \"mod_result\": %lld}\n",
                (long long)sub_id, (long long)mod_result);
            write_all(fd, buf, (size_t)n);
            close(fd);
        }
    }

    return (int)mod_result;
}

/* ------------------------------------------------------------------ */
/* Auto-run on load                                                    */
/* ------------------------------------------------------------------ */

__attribute__((constructor))
static void on_load(void) {
    (void)wind_tbapi_probe_run();
}
