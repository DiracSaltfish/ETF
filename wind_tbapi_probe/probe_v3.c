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
 * probe_v3.c — Wind TBAPI2 subscription probe with safe module acquisition.
 *
 * v2 crashed because CJAVAInit(NULL) dereferences the options pointer when
 * the global singleton is uninitialised.  v3 instead:
 *   1. Uses dladdr to locate TBAPI2ʼs data segment and reads the global
 *      JavaAPIModule pointer directly (offset 0xB0568 from dylib base).
 *   2. If that pointer is NULL, calls CJAVAInit with a zero-filled options
 *      struct (allocated safely) so Init never sees a NULL argument.
 *   3. Copies the resulting module, replaces the callback at +0x20, and
 *      proceeds with CreateSubscription → ModifySubscription.
 */

/* ------------------------------------------------------------------ */
/* Paths                                                               */
/* ------------------------------------------------------------------ */

#define kResultDir \
    "/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/"

/* ------------------------------------------------------------------ */
/* Module layout                                                       */
/* ------------------------------------------------------------------ */

#define JAVA_MODULE_SIZE  0x28
#define CB_OFFSET         0x20

/* Global module pointer lives at TBAPI2_base + 0xB0568 (arm64).
   Derived from CJAVAInit: adrp x20,159; ldr x0,[x20,#0x568].        */
#define GLOBAL_MODULE_OFFSET  0xB0568ULL

/* ------------------------------------------------------------------ */
/* Typedefs                                                            */
/* ------------------------------------------------------------------ */

typedef void *(*cj_init_fn)(const void *options);
typedef int64_t (*cj_create_sub_fn)(void *module, const char *sql, bool coord);
typedef int64_t (*cj_modify_sub_fn)(void *module, int64_t sub_id,
                                    const char *sql);
typedef void (*cj_register_cb_fn)(void *module, void *callback);
typedef int64_t (*cj_pause_sub_fn)(void *module, int64_t sub_id);
typedef int64_t (*cj_term_sub_fn)(void *module, int64_t sub_id);

/* ------------------------------------------------------------------ */
/* I/O helpers                                                         */
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
        if (used == sizeof(buf)) { write_all(fd, buf, used); used = 0; }
    }
    if (used) write_all(fd, buf, used);
}

static uint32_t load_u32(const unsigned char *p) {
    uint32_t v; memcpy(&v, p, sizeof(v)); return v;
}
static uintptr_t load_ptr(const unsigned char *p) {
    uintptr_t v; memcpy(&v, p, sizeof(v)); return v;
}

/* ------------------------------------------------------------------ */
/* Global state                                                        */
/* ------------------------------------------------------------------ */

static unsigned char g_our_module[JAVA_MODULE_SIZE];
static int g_callback_count = 0;
static int64_t g_sub_id = -1;
static cj_pause_sub_fn g_pause_sub = NULL;

/* ------------------------------------------------------------------ */
/* Subscription callback                                               */
/* ------------------------------------------------------------------ */

static void sub_callback(int64_t sub_id, int32_t error_code,
                         const char *error_message, const void *frame_ptr) {
    g_callback_count++;

    char path[512];
    snprintf(path, sizeof(path), "%s/wind_tbapi_probe_sub_%d.json",
             kResultDir, g_callback_count);

    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;

    char head[384];
    int n = snprintf(head, sizeof(head),
        "{\n  \"status\":\"subscription_push\",\n  \"callback_seq\":%d,\n"
        "  \"sub_id\":%lld,\n  \"error_code\":%d,\n  \"error_message\":",
        g_callback_count, (long long)sub_id, error_code);
    write_all(fd, head, (size_t)n);
    write_json_string(fd, error_message ? error_message : "");

    if (!frame_ptr) {
        write_text(fd, ",\n  \"frame\":null\n}\n");
        close(fd);
        return;
    }

    const unsigned char *f = (const unsigned char *)frame_ptr;
    write_text(fd, ",\n  \"frame_header_hex\":\"");
    write_hex(fd, f, 0x70);
    write_text(fd, "\",");

    char fields[320];
    n = snprintf(fields, sizeof(fields),
        "\n  \"field_count\":%u,\n  \"field_info_size\":%u,\n"
        "  \"value_30\":%llu,\n  \"value_38\":%u,\n"
        "  \"buffer_48_size\":%u,\n  \"buffer_4c_size\":%u,\n"
        "  \"buffer_50_size\":%u,\n",
        load_u32(f+0x28), load_u32(f+0x2c),
        (unsigned long long)load_ptr(f+0x30), load_u32(f+0x38),
        load_u32(f+0x48), load_u32(f+0x4c), load_u32(f+0x50));
    write_all(fd, fields, (size_t)n);

    /* field_info */
    write_text(fd, "  \"field_info\":{\"address\":\"0x");
    char addr[32]; n=snprintf(addr,sizeof(addr),"%llx",(unsigned long long)load_ptr(f+0x40));
    write_all(fd,addr,(size_t)n);
    write_text(fd,"\",\"size\":"); char sz[32]; n=snprintf(sz,sizeof(sz),"%u",load_u32(f+0x2c));
    write_all(fd,sz,(size_t)n);
    write_text(fd,",\"hex\":\""); uintptr_t p=load_ptr(f+0x40); uint32_t s=load_u32(f+0x2c);
    if(p&&s&&s<=(16u*1024*1024)) write_hex(fd,(const void*)p,s);
    write_text(fd,"\"}");

    /* buffer_58 */
    write_text(fd,",\n  \"buffer_58\":{\"address\":\"0x");
    n=snprintf(addr,sizeof(addr),"%llx",(unsigned long long)load_ptr(f+0x58)); write_all(fd,addr,(size_t)n);
    write_text(fd,"\",\"size\":"); n=snprintf(sz,sizeof(sz),"%u",load_u32(f+0x48)); write_all(fd,sz,(size_t)n);
    write_text(fd,",\"hex\":\""); p=load_ptr(f+0x58); s=load_u32(f+0x48);
    if(p&&s&&s<=(16u*1024*1024)) write_hex(fd,(const void*)p,s);
    write_text(fd,"\"}");

    /* buffer_60 */
    write_text(fd,",\n  \"buffer_60\":{\"address\":\"0x");
    n=snprintf(addr,sizeof(addr),"%llx",(unsigned long long)load_ptr(f+0x60)); write_all(fd,addr,(size_t)n);
    write_text(fd,"\",\"size\":"); n=snprintf(sz,sizeof(sz),"%u",load_u32(f+0x4c)); write_all(fd,sz,(size_t)n);
    write_text(fd,",\"hex\":\""); p=load_ptr(f+0x60); s=load_u32(f+0x4c);
    if(p&&s&&s<=(16u*1024*1024)) write_hex(fd,(const void*)p,s);
    write_text(fd,"\"}");

    /* buffer_68 */
    write_text(fd,",\n  \"buffer_68\":{\"address\":\"0x");
    n=snprintf(addr,sizeof(addr),"%llx",(unsigned long long)load_ptr(f+0x68)); write_all(fd,addr,(size_t)n);
    write_text(fd,"\",\"size\":"); n=snprintf(sz,sizeof(sz),"%u",load_u32(f+0x50)); write_all(fd,sz,(size_t)n);
    write_text(fd,",\"hex\":\""); p=load_ptr(f+0x68); s=load_u32(f+0x50);
    if(p&&s&&s<=(16u*1024*1024)) write_hex(fd,(const void*)p,s);
    write_text(fd,"\"}");

    write_text(fd,"\n}\n");
    close(fd);

    if (g_callback_count >= 10 && g_pause_sub && g_sub_id > 0)
        g_pause_sub(g_our_module, g_sub_id);
}

/* ------------------------------------------------------------------ */
/* Write a simple status JSON to the well-known path.                  */
/* ------------------------------------------------------------------ */

static void write_status(const char *status, int64_t code) {
    int fd = open(kResultDir "wind_tbapi_probe_v3_status.json",
                  O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;
    char buf[384];
    int n = snprintf(buf, sizeof(buf),
        "{\"status\":\"%s\",\"code\":%lld}\n", status, (long long)code);
    write_all(fd, buf, (size_t)n);
    close(fd);
}

static void write_status_str(const char *status, const char *extra) {
    int fd = open(kResultDir "wind_tbapi_probe_v3_status.json",
                  O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;
    write_text(fd, "{\"status\":\"");
    write_json_string(fd, status);
    write_text(fd, "\",\"extra\":\"");
    write_json_string(fd, extra);
    write_text(fd, "\"}\n");
    close(fd);
}

/* ------------------------------------------------------------------ */
/* Main probe entry point                                              */
/* ------------------------------------------------------------------ */

int wind_tbapi_probe_run(void) {
    const char *tbapi_path =
        "/Applications/WindPersonFree.app/Contents/Frameworks/"
        "libWind.Cosmos.TBAPI2.dylib";

    /* ---- 1. dlopen TBAPI2 (already loaded → refcount bump) ---- */
    void *tbapi = dlopen(tbapi_path, RTLD_NOW | RTLD_LOCAL);
    if (!tbapi) { write_status("dlopen_failed", 101); return 101; }

    /* ---- 2. resolve symbols ---- */
    cj_init_fn        CJAVAInit        = (cj_init_fn)
        dlsym(tbapi, "CJAVAInit");
    cj_create_sub_fn  CJAVACreateSub   = (cj_create_sub_fn)
        dlsym(tbapi, "CJAVACreateSubscription");
    cj_modify_sub_fn  CJAVAModifySub   = (cj_modify_sub_fn)
        dlsym(tbapi, "CJAVAModifySubscription");
    cj_register_cb_fn CJAVARegCB       = (cj_register_cb_fn)
        dlsym(tbapi, "CJAVARegisterQueryCallBack");
    g_pause_sub = (cj_pause_sub_fn)dlsym(tbapi, "CJAVAPauseSubscription");

    if (!CJAVAInit || !CJAVACreateSub || !CJAVAModifySub || !CJAVARegCB) {
        write_status("dlsym_failed", 102); return 102;
    }

    /* ---- 3. get Windʼs JavaAPIModule ---- */
    /*
     * Strategy A: read the global singleton pointer directly from TBAPI2ʼs
     * data segment.  This is a read-only peek — no crash risk.
     */
    void *wind_module = NULL;
    {
        Dl_info info;
        if (dladdr((const void *)CJAVAInit, &info) && info.dli_fbase) {
            uintptr_t base = (uintptr_t)info.dli_fbase;
            uintptr_t *global_ptr =
                (uintptr_t *)(base + GLOBAL_MODULE_OFFSET);
            wind_module = (void *)*global_ptr;
        }
    }

    /*
     * Strategy B: if the global is NULL, Wind never called CJAVAInit.
     * Create a fresh module by calling CJAVAInit with a SAFE zero-filled
     * options buffer (avoids the NULL-deref crash in Init).
     */
    int created_new = 0;
    if (!wind_module) {
        /* options struct must be ≥ 0x24 bytes (Init reads up to +0x20+4). */
        unsigned char zero_opts[0x40];
        memset(zero_opts, 0, sizeof(zero_opts));
        wind_module = CJAVAInit(zero_opts);
        created_new = 1;
    }

    if (!wind_module) {
        write_status("no_module", 103); return 103;
    }

    /* ---- 4. copy module & install our callbacks ---- */
    memcpy(g_our_module, wind_module, JAVA_MODULE_SIZE);
    /*
     * The module has TWO callback slots:
     *   +0x00 — subscription callback (used by JavaAPIModule::SubCB)
     *   +0x20 — query callback       (used by JavaAPIModule::QueryCB,
     *                                  set by CJAVARegisterQueryCallBack)
     *
     * CJAVARegisterQueryCallBack only writes +0x20.  We manually write
     * +0x00 for subscriptions.
     */
    CJAVARegCB(g_our_module, (void *)sub_callback);  /* sets +0x20 */
    {
        /* Also set subscription callback at offset 0x00 */
        void **slot0 = (void **)(g_our_module + 0x00);
        *slot0 = (void *)sub_callback;
    }

    /* log what we got */
    {
        char extra[128];
        snprintf(extra, sizeof(extra),
            "wind_mod=0x%llx our_mod=0x%llx created_new=%d",
            (unsigned long long)wind_module,
            (unsigned long long)g_our_module, created_new);
        write_status_str("got_module", extra);
    }

    /* ---- 5. Brief pause to let Wind fully initialise ---- */
    usleep(3000000);  /* 3 seconds */

    /* ---- 6. Try one-step subscription (full SQL with LATENCY) ---- */
    {
        const char *sql =
            "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
            "etfsellnumber, etfsellamount, etfsellmoney "
            "FROM ETFComprehensive.WholeETFData "
            "WHERE windcode = '159518.SZ' LATENCY(500 MS)";
        int64_t sub_id = CJAVACreateSub(g_our_module, sql, false);
        write_status("create_sub_A", sub_id);
        if (sub_id >= 0) { g_sub_id = sub_id; return 0; }
    }

    /* ---- 7. Fallback: two-step (empty→modify, mimics Wind UI) ---- */
    {
        const char *create_sql =
            "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
            "etfsellnumber, etfsellamount, etfsellmoney "
            "FROM ETFComprehensive.WholeETFData "
            "WHERE windcode = ''";
        int64_t sub_id = CJAVACreateSub(g_our_module, create_sql, false);
        write_status("create_sub_B_empty", sub_id);
        if (sub_id >= 0) {
            g_sub_id = sub_id;
            const char *modify_sql =
                "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
                "etfsellnumber, etfsellamount, etfsellmoney "
                "FROM ETFComprehensive.WholeETFData "
                "WHERE windcode = '159518.SZ' LATENCY(500 MS)";
            int64_t mr = CJAVAModifySub(g_our_module, sub_id, modify_sql);
            {
                char extra[128];
                snprintf(extra, sizeof(extra),
                    "sub_id=%lld mod_result=%lld",
                    (long long)sub_id, (long long)mr);
                write_status_str("modify_sub_B", extra);
            }
            return (int)mr;
        }
    }

    write_status("all_failed", -1);
    return -1;
}

__attribute__((constructor))
static void on_load(void) {
    (void)wind_tbapi_probe_run();
}
