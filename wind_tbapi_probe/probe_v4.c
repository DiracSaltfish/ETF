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
 * probe_v4.c — Try multiple SQL formats to find the right one.
 *
 * v3 showed that CreateSubscription returns -12293 ("ParseSql Fail").
 * This version tries several approaches:
 *   A) Full SQL with LATENCY (one-step, skip Modify)
 *   B) Full SQL without LATENCY
 *   C) Just table.column reference format
 *   D) Wind UI's two-step: empty windcode → modify
 *
 * It also dumps the error string from dlerror() and writes each attempt
 * result to a separate status file.
 */

#define kResultDir \
    "/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/"

#define JAVA_MODULE_SIZE  0x28
#define GLOBAL_MODULE_OFFSET  0xB0568ULL

typedef void *(*cj_init_fn)(const void *options);
typedef int64_t (*cj_create_sub_fn)(void *module, const char *sql, bool coord);
typedef int64_t (*cj_modify_sub_fn)(void *module, int64_t sub_id,
                                    const char *sql);
typedef void (*cj_register_cb_fn)(void *module, void *callback);
typedef int64_t (*cj_pause_sub_fn)(void *module, int64_t sub_id);
typedef int64_t (*cj_term_sub_fn)(void *module, int64_t sub_id);

static void write_all(int fd, const void *data, size_t size) {
    const unsigned char *p = (const unsigned char *)data;
    while (size > 0) {
        ssize_t n = write(fd, p, size);
        if (n < 0) { if (errno == EINTR) continue; return; }
        p += (size_t)n; size -= (size_t)n;
    }
}
static void write_text(int fd, const char *s) { write_all(fd, s, strlen(s)); }
static void write_json_string(int fd, const char *s) {
    write_text(fd, "\"");
    if (s) for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
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
                } else write_all(fd, p, 1);
        }
    }
    write_text(fd, "\"");
}
static void write_hex(int fd, const void *data, size_t size) {
    static const char d[] = "0123456789abcdef";
    const unsigned char *p = (const unsigned char *)data;
    char buf[4096]; size_t u = 0;
    for (size_t i = 0; i < size; ++i) {
        buf[u++] = d[p[i]>>4]; buf[u++] = d[p[i]&0xf];
        if (u == sizeof(buf)) { write_all(fd, buf, u); u = 0; }
    }
    if (u) write_all(fd, buf, u);
}
static uint32_t load_u32(const unsigned char *p) {
    uint32_t v; memcpy(&v, p, sizeof(v)); return v;
}
static uintptr_t load_ptr(const unsigned char *p) {
    uintptr_t v; memcpy(&v, p, sizeof(v)); return v;
}

static unsigned char g_our_module[JAVA_MODULE_SIZE];
static int g_callback_count = 0;
static int64_t g_sub_id = -1;
static cj_pause_sub_fn g_pause_sub = NULL;

/* ---- subscription callback ---- */
static void sub_callback(int64_t sub_id, int32_t error_code,
                         const char *error_message, const void *frame_ptr) {
    g_callback_count++;
    char path[512];
    snprintf(path, sizeof(path), "%s/wind_tbapi_probe_v4_sub_%d.json",
             kResultDir, g_callback_count);
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;

    char head[384];
    int n = snprintf(head, sizeof(head),
        "{\n  \"status\":\"push\",\n  \"seq\":%d,\n"
        "  \"sub_id\":%lld,\n  \"error_code\":%d,\n  \"error_message\":",
        g_callback_count, (long long)sub_id, error_code);
    write_all(fd, head, (size_t)n);
    write_json_string(fd, error_message ? error_message : "");

    if (!frame_ptr) { write_text(fd, ",\n  \"frame\":null\n}\n"); close(fd); return; }

    const unsigned char *f = (const unsigned char *)frame_ptr;
    write_text(fd, ",\n  \"frame_header_hex\":\"");
    write_hex(fd, f, 0x70);
    write_text(fd, "\",\n  \"field_count\":");
    char tmp[64]; n=snprintf(tmp,sizeof(tmp),"%u",load_u32(f+0x28)); write_all(fd,tmp,(size_t)n);
    write_text(fd, ",\n  \"field_info_size\":");
    n=snprintf(tmp,sizeof(tmp),"%u",load_u32(f+0x2c)); write_all(fd,tmp,(size_t)n);
    write_text(fd, ",\n  \"buffer_48_size\":");
    n=snprintf(tmp,sizeof(tmp),"%u",load_u32(f+0x48)); write_all(fd,tmp,(size_t)n);
    write_text(fd, ",\n  \"buffer_4c_size\":");
    n=snprintf(tmp,sizeof(tmp),"%u",load_u32(f+0x4c)); write_all(fd,tmp,(size_t)n);
    write_text(fd, ",\n  \"buffer_50_size\":");
    n=snprintf(tmp,sizeof(tmp),"%u",load_u32(f+0x50)); write_all(fd,tmp,(size_t)n);

    /* dump field_info buffer */
    write_text(fd, ",\n  \"field_info_hex\":\"");
    uintptr_t fi = load_ptr(f+0x40); uint32_t fs = load_u32(f+0x2c);
    if (fi && fs && fs <= 0x100000) write_hex(fd, (const void*)fi, fs);
    write_text(fd, "\"");

    /* dump data buffers */
    for (int i = 0; i < 3; i++) {
        uint32_t off_b = i == 0 ? 0x48 : i == 1 ? 0x4c : 0x50;
        uint32_t off_p = i == 0 ? 0x58 : i == 1 ? 0x60 : 0x68;
        write_text(fd, ",\n  \"buf_");
        n=snprintf(tmp,sizeof(tmp),"%x_hex\":\"", off_p); write_all(fd,tmp,(size_t)n);
        uintptr_t bp = load_ptr(f+off_p); uint32_t bs = load_u32(f+off_b);
        if (bp && bs && bs <= 0x100000) write_hex(fd, (const void*)bp, bs);
        write_text(fd, "\"");
    }
    write_text(fd, "\n}\n"); close(fd);

    if (g_callback_count >= 10 && g_pause_sub && g_sub_id > 0)
        g_pause_sub(g_our_module, g_sub_id);
}

/* ---- write result ---- */
static void write_result(const char *attempt, int64_t code, const char *sql) {
    char path[256];
    snprintf(path, sizeof(path), "%s/wind_tbapi_probe_v4_%s.json",
             kResultDir, attempt);
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;
    write_text(fd, "{\n  \"attempt\":\"");
    write_json_string(fd, attempt);
    write_text(fd, "\",\n  \"code\":");
    char tmp[64]; int n = snprintf(tmp, sizeof(tmp), "%lld", (long long)code);
    write_all(fd, tmp, (size_t)n);
    write_text(fd, ",\n  \"sql\":\"");
    write_json_string(fd, sql);
    write_text(fd, "\"\n}\n");
    close(fd);
}

/* ---- entry ---- */
int wind_tbapi_probe_run(void) {
    const char *tbapi_path =
        "/Applications/WindPersonFree.app/Contents/Frameworks/"
        "libWind.Cosmos.TBAPI2.dylib";

    void *tbapi = dlopen(tbapi_path, RTLD_NOW | RTLD_LOCAL);
    if (!tbapi) { write_result("dlopen", 101, ""); return 101; }

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
        write_result("dlsym", 102, ""); return 102;
    }

    /* get module (same safe logic as v3) */
    void *wind_module = NULL;
    {
        Dl_info info;
        if (dladdr((const void *)CJAVAInit, &info) && info.dli_fbase) {
            uintptr_t *gp = (uintptr_t *)((uintptr_t)info.dli_fbase + GLOBAL_MODULE_OFFSET);
            wind_module = (void *)*gp;
        }
    }
    int created_new = 0;
    if (!wind_module) {
        unsigned char zero_opts[0x40];
        memset(zero_opts, 0, sizeof(zero_opts));
        wind_module = CJAVAInit(zero_opts);
        created_new = 1;
    }
    if (!wind_module) { write_result("no_module", 103, ""); return 103; }

    memcpy(g_our_module, wind_module, JAVA_MODULE_SIZE);
    CJAVARegCB(g_our_module, (void *)sub_callback);

    /* ---- log module info ---- */
    {
        char extra[256];
        snprintf(extra, sizeof(extra),
            "{\"wind_mod\":\"0x%llx\",\"our_mod\":\"0x%llx\",\"created_new\":%d}",
            (unsigned long long)wind_module,
            (unsigned long long)g_our_module, created_new);
        write_result("module", 0, extra);
    }

    /* ---- attempt A: one-step full SQL with LATENCY ---- */
    {
        const char *sql =
            "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
            "etfsellnumber, etfsellamount, etfsellmoney "
            "FROM ETFComprehensive.WholeETFData "
            "WHERE windcode = '159518.SZ' LATENCY(500 MS)";
        int64_t r = CJAVACreateSub(g_our_module, sql, false);
        write_result("A_full_latency", r, sql);
        if (r >= 0) { g_sub_id = r; return 0; }
    }

    /* ---- attempt B: full SQL WITHOUT LATENCY ---- */
    {
        const char *sql =
            "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
            "etfsellnumber, etfsellamount, etfsellmoney "
            "FROM ETFComprehensive.WholeETFData "
            "WHERE windcode = '159518.SZ'";
        int64_t r = CJAVACreateSub(g_our_module, sql, false);
        write_result("B_full_no_latency", r, sql);
        if (r >= 0) { g_sub_id = r; return 0; }
    }

    /* ---- attempt C: two-step (v3 approach, empty→modify) ---- */
    {
        const char *create_sql =
            "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
            "etfsellnumber, etfsellamount, etfsellmoney "
            "FROM ETFComprehensive.WholeETFData "
            "WHERE windcode = ''";
        int64_t sub_id = CJAVACreateSub(g_our_module, create_sql, false);
        write_result("C_create_empty", sub_id, create_sql);
        if (sub_id >= 0) {
            const char *modify_sql =
                "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
                "etfsellnumber, etfsellamount, etfsellmoney "
                "FROM ETFComprehensive.WholeETFData "
                "WHERE windcode = '159518.SZ' LATENCY(500 MS)";
            int64_t mr = CJAVAModifySub(g_our_module, sub_id, modify_sql);
            write_result("C_modify", mr, modify_sql);
            if (mr >= 0) { g_sub_id = sub_id; return 0; }
        }
    }

    /* ---- attempt D: minimal SQL - just WHERE clause? ---- */
    {
        const char *sql =
            "windcode = '159518.SZ'";
        int64_t r = CJAVACreateSub(g_our_module, sql, true);
        write_result("D_where_only", r, sql);
        if (r >= 0) { g_sub_id = r; return 0; }
    }

    write_result("all_failed", -1, "no approach succeeded");
    return -1;
}

__attribute__((constructor))
static void on_load(void) {
    (void)wind_tbapi_probe_run();
}
