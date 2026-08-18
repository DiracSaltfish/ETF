#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

/*
 * Minimal, read-only probe for the already-loaded Wind TBAPI2 client.
 * It intentionally does not initialize or authenticate a second client.
 */

static const char *kResultPath =
    "/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/wind_tbapi_probe_result.json";
static unsigned char g_java_module[0x28];

typedef void (*register_callback_fn)(void *, void *);
typedef int64_t (*query_fn)(void *, int64_t, const char *, uint32_t, bool);

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

static void write_buffer_field(int fd, const char *name, uintptr_t ptr,
                               uint32_t size, bool comma) {
    char meta[160];
    if (comma) write_text(fd, ",\n");
    int n = snprintf(meta, sizeof(meta),
                     "  \"%s\": {\"address\": \"0x%llx\", \"size\": %u, \"hex\": \"",
                     name, (unsigned long long)ptr, size);
    write_all(fd, meta, (size_t)n);
    if (ptr && size && size <= (16u * 1024u * 1024u)) {
        write_hex(fd, (const void *)ptr, size);
    }
    write_text(fd, "\"}");
}

/* QueryCB calls this synchronously before freeing the JavaTableFrame buffers. */
static void query_callback(int64_t request_id, int32_t error_code,
                           const char *error_message, const void *frame_ptr) {
    int fd = open(kResultPath, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;

    char head[192];
    int n = snprintf(head, sizeof(head),
                     "{\n  \"status\": \"callback\",\n  \"request_id\": %lld,\n  \"error_code\": %d,\n  \"error_message\": ",
                     (long long)request_id, error_code);
    write_all(fd, head, (size_t)n);
    write_json_string(fd, error_message ? error_message : "");

    if (!frame_ptr) {
        write_text(fd, ",\n  \"frame\": null\n}\n");
        close(fd);
        return;
    }

    const unsigned char *f = (const unsigned char *)frame_ptr;
    write_text(fd, ",\n  \"frame_header_hex\": \"");
    write_hex(fd, f, 0x70);
    write_text(fd, "\",\n");

    char fields[256];
    n = snprintf(fields, sizeof(fields),
                 "  \"field_count\": %u,\n  \"field_info_size\": %u,\n"
                 "  \"value_30\": %llu,\n  \"value_38\": %u,\n"
                 "  \"buffer_48_size\": %u,\n  \"buffer_4c_size\": %u,\n"
                 "  \"buffer_50_size\": %u,\n",
                 load_u32(f + 0x28), load_u32(f + 0x2c),
                 (unsigned long long)load_ptr(f + 0x30), load_u32(f + 0x38),
                 load_u32(f + 0x48), load_u32(f + 0x4c), load_u32(f + 0x50));
    write_all(fd, fields, (size_t)n);

    /* These relationships follow JavaAPIModule::GetJaveTableFrame. */
    write_buffer_field(fd, "field_info", load_ptr(f + 0x40), load_u32(f + 0x2c), false);
    write_buffer_field(fd, "buffer_58", load_ptr(f + 0x58), load_u32(f + 0x48), true);
    write_buffer_field(fd, "buffer_60", load_ptr(f + 0x60), load_u32(f + 0x4c), true);
    write_buffer_field(fd, "buffer_68", load_ptr(f + 0x68), load_u32(f + 0x50), true);
    write_text(fd, "\n}\n");
    close(fd);
}

__attribute__((visibility("default")))
int wind_tbapi_probe_run(void) {
    const char *tbapi_path =
        "/Applications/WindPersonFree.app/Contents/Frameworks/libWind.Cosmos.TBAPI2.dylib";
    void *tbapi = dlopen(tbapi_path, RTLD_NOW | RTLD_LOCAL);
    if (!tbapi) return 101;

    register_callback_fn register_callback =
        (register_callback_fn)dlsym(tbapi, "CJAVARegisterQueryCallBack");
    query_fn query = (query_fn)dlsym(tbapi, "CJAVAQuery");
    if (!register_callback || !query) return 102;

    memset(g_java_module, 0, sizeof(g_java_module));
    register_callback(g_java_module, (void *)query_callback);

    const char *sql =
        "SELECT etfbuynumber, etfbuyamount, etfbuymoney, "
        "etfsellnumber, etfsellamount, etfsellmoney "
        "FROM ETFComprehensive.WholeETFData "
        "WHERE windcode = '159518.SZ' LATENCY(500 MS)";

    int fd = open(kResultPath, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd >= 0) {
        write_text(fd, "{\n  \"status\": \"submitted\"\n}\n");
        close(fd);
    }

    int64_t result = query(g_java_module, 159518, sql, 5000, false);
    if (result < 0) {
        fd = open(kResultPath, O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (fd >= 0) {
            char msg[128];
            int len = snprintf(msg, sizeof(msg),
                               "{\n  \"status\": \"submit_error\",\n  \"code\": %lld\n}\n",
                               (long long)result);
            write_all(fd, msg, (size_t)len);
            close(fd);
        }
    }
    return (int)result;
}

__attribute__((constructor))
static void on_load(void) {
    (void)wind_tbapi_probe_run();
}
