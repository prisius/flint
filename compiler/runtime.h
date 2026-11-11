/* ===== Flint runtime (inlined into every generated C file) ===== */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <math.h>
#include <time.h>

#define FL_NORETURN __attribute__((noreturn, cold, noinline))
#define FL_UNUSED __attribute__((unused))

typedef struct fl_str { char* ptr; int64_t len; } fl_str;
#define FL_STR(lit, n) ((fl_str){(char*)(lit), (n)})

#define FL_SLICE_TYPE(M, T) typedef struct fl_slice_##M { T* ptr; int64_t len; } fl_slice_##M;
#define FL_LIST_TYPE(M, T) typedef struct fl_list_##M { T* ptr; int64_t len; int64_t cap; } fl_list_##M;

/* ---------- panics ---------- */
static FL_NORETURN void fl_panic_cstr(const char* loc, const char* msg) {
    fflush(stdout);
    fprintf(stderr, "%s: panic: %s\n", loc, msg);
    exit(101);
}
static FL_NORETURN void fl_panic_str(const char* loc, fl_str msg) {
    fflush(stdout);
    fprintf(stderr, "%s: panic: %.*s\n", loc, (int)msg.len, msg.ptr);
    exit(101);
}
static FL_NORETURN void fl_panic_idx(int64_t i, int64_t len, const char* loc) {
    fflush(stdout);
    fprintf(stderr, "%s: panic: index out of bounds: index %lld, length %lld\n", loc, (long long)i, (long long)len);
    exit(101);
}
static FL_NORETURN void fl_assert_fail(const char* loc, const char* expr, fl_str msg) {
    fflush(stdout);
    if (msg.len) fprintf(stderr, "%s: assertion failed: %.*s\n", loc, (int)msg.len, msg.ptr);
    else fprintf(stderr, "%s: assertion failed: %s\n", loc, expr);
    exit(101);
}

static inline int64_t fl_chk(int64_t i, int64_t len, const char* loc) {
#ifndef FL_NOCHECK
    if (__builtin_expect((uint64_t)i >= (uint64_t)len, 0)) fl_panic_idx(i, len, loc);
#endif
    (void)loc;
    return i;
}
static inline void fl_chk_range(int64_t lo, int64_t hi, int64_t len, const char* loc) {
#ifndef FL_NOCHECK
    if (__builtin_expect(lo < 0 || hi < lo || hi > len, 0)) {
        fflush(stdout);
        fprintf(stderr, "%s: panic: slice bounds out of range [%lld..%lld] with length %lld\n", loc, (long long)lo, (long long)hi, (long long)len);
        exit(101);
    }
#endif
    (void)lo; (void)hi; (void)len; (void)loc;
}

/* ---------- memory ---------- */
static void* fl_alloc(int64_t size) {
    void* p = calloc(1, size > 0 ? (size_t)size : 1);
    if (!p) fl_panic_cstr("runtime", "out of memory");
    return p;
}
static void* fl_realloc(void* p, int64_t size) {
    p = realloc(p, size > 0 ? (size_t)size : 1);
    if (!p) fl_panic_cstr("runtime", "out of memory");
    return p;
}

/* ---------- lists ---------- */
#define FL_LIST_IMPL(M, T) \
static FL_UNUSED void fl_list_##M##_reserve(fl_list_##M* l, int64_t cap) { \
    if (cap > l->cap) { l->ptr = (T*)fl_realloc(l->ptr, cap * (int64_t)sizeof(T)); l->cap = cap; } } \
static FL_UNUSED inline void fl_list_##M##_push(fl_list_##M* l, T v) { \
    if (__builtin_expect(l->len == l->cap, 0)) fl_list_##M##_reserve(l, l->cap ? l->cap * 2 : 8); \
    l->ptr[l->len++] = v; } \
static FL_UNUSED inline T fl_list_##M##_pop(fl_list_##M* l, const char* loc) { \
    if (l->len <= 0) fl_panic_cstr(loc, "pop from empty list"); \
    return l->ptr[--l->len]; } \
static FL_UNUSED inline void fl_list_##M##_clear(fl_list_##M* l) { l->len = 0; } \
static FL_UNUSED T fl_list_##M##_remove(fl_list_##M* l, int64_t i, const char* loc) { \
    fl_chk(i, l->len, loc); T v = l->ptr[i]; \
    memmove(l->ptr + i, l->ptr + i + 1, (size_t)(l->len - i - 1) * sizeof(T)); \
    l->len--; return v; } \
static FL_UNUSED void fl_list_##M##_insert(fl_list_##M* l, int64_t i, T v, const char* loc) { \
    if ((uint64_t)i > (uint64_t)l->len) fl_panic_idx(i, l->len, loc); \
    if (l->len == l->cap) fl_list_##M##_reserve(l, l->cap ? l->cap * 2 : 8); \
    memmove(l->ptr + i + 1, l->ptr + i, (size_t)(l->len - i) * sizeof(T)); \
    l->ptr[i] = v; l->len++; } \
static FL_UNUSED void fl_list_##M##_free(fl_list_##M* l) { free(l->ptr); l->ptr = NULL; l->len = l->cap = 0; }

FL_SLICE_TYPE(str, fl_str)
FL_LIST_TYPE(str, fl_str)
FL_LIST_IMPL(str, fl_str)

/* ---------- string builder (used by print and "{interpolation}") ---------- */
typedef struct fl_sb { char* ptr; int64_t len; int64_t cap; char buf[256]; } fl_sb;

static inline void fl_sb_init(fl_sb* b) { b->ptr = b->buf; b->len = 0; b->cap = (int64_t)sizeof b->buf; }
static void fl_sb_grow(fl_sb* b, int64_t extra) {
    int64_t need = b->len + extra;
    if (need <= b->cap) return;
    int64_t cap = b->cap * 2;
    while (cap < need) cap *= 2;
    if (b->ptr == b->buf) {
        char* p = (char*)fl_alloc(cap);
        memcpy(p, b->buf, (size_t)b->len);
        b->ptr = p;
    } else {
        b->ptr = (char*)fl_realloc(b->ptr, cap);
    }
    b->cap = cap;
}
static inline void fl_sb_put(fl_sb* b, const char* s, int64_t n) {
    if (b->len + n > b->cap) fl_sb_grow(b, n);
    memcpy(b->ptr + b->len, s, (size_t)n);
    b->len += n;
}
static inline void fl_sb_str(fl_sb* b, fl_str s) { fl_sb_put(b, s.ptr, s.len); }
static inline void fl_sb_char(fl_sb* b, char c) { fl_sb_put(b, &c, 1); }
static void fl_sb_printf(fl_sb* b, const char* fmt, ...) __attribute__((format(printf, 2, 3)));
#include <stdarg.h>
static void fl_sb_printf(fl_sb* b, const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(b->ptr + b->len, (size_t)(b->cap - b->len), fmt, ap);
    va_end(ap);
    if (n >= b->cap - b->len) {
        fl_sb_grow(b, n + 1);
        va_start(ap, fmt);
        vsnprintf(b->ptr + b->len, (size_t)(b->cap - b->len), fmt, ap);
        va_end(ap);
    }
    b->len += n;
}
static void fl_sb_i64(fl_sb* b, int64_t v) {
    char tmp[24]; int i = 24;
    uint64_t u = v < 0 ? (uint64_t)0 - (uint64_t)v : (uint64_t)v;
    do { tmp[--i] = (char)('0' + u % 10); u /= 10; } while (u);
    if (v < 0) tmp[--i] = '-';
    fl_sb_put(b, tmp + i, 24 - i);
}
static void fl_sb_u64(fl_sb* b, uint64_t u) {
    char tmp[24]; int i = 24;
    do { tmp[--i] = (char)('0' + u % 10); u /= 10; } while (u);
    fl_sb_put(b, tmp + i, 24 - i);
}
static void fl_sb_bin(fl_sb* b, uint64_t u, int width, bool zero, bool left) {
    char tmp[64]; int i = 64;
    do { tmp[--i] = (char)('0' + (u & 1)); u >>= 1; } while (u);
    int n = 64 - i;
    if (!left) while (n < width) { fl_sb_char(b, zero ? '0' : ' '); width--; }
    fl_sb_put(b, tmp + i, n);
    if (left) while (n < width) { fl_sb_char(b, ' '); width--; }
}
static void fl_sb_float(fl_sb* b, double v, int maxprec) {
    char buf[40];
    if (v != v) { fl_sb_put(b, "nan", 3); return; }
    if (v == HUGE_VAL) { fl_sb_put(b, "inf", 3); return; }
    if (v == -HUGE_VAL) { fl_sb_put(b, "-inf", 4); return; }
    int n = 0;
    for (int p = maxprec - 2; p <= maxprec; p++) {   /* shortest that round-trips */
        n = snprintf(buf, sizeof buf, "%.*g", p, v);
        if (maxprec > 9 ? strtod(buf, NULL) == v : strtof(buf, NULL) == (float)v) break;
    }
    fl_sb_put(b, buf, n);
    if (!strpbrk(buf, ".en")) fl_sb_put(b, ".0", 2);
}
static inline void fl_sb_f64(fl_sb* b, double v) { fl_sb_float(b, v, 17); }
static inline void fl_sb_f32(fl_sb* b, float v) { fl_sb_float(b, (double)v, 9); }
static inline void fl_sb_bool(fl_sb* b, bool v) { if (v) fl_sb_put(b, "true", 4); else fl_sb_put(b, "false", 5); }
static void fl_sb_ptr(fl_sb* b, const void* p) { if (p) fl_sb_printf(b, "%p", p); else fl_sb_put(b, "null", 4); }
static void fl_sb_strq(fl_sb* b, fl_str s) { fl_sb_char(b, '"'); fl_sb_str(b, s); fl_sb_char(b, '"'); }
static void fl_sb_charq(fl_sb* b, char c) { fl_sb_char(b, '\''); fl_sb_char(b, c); fl_sb_char(b, '\''); }

static void fl_sb_flush(fl_sb* b, FILE* f) {
    fwrite(b->ptr, 1, (size_t)b->len, f);
    if (b->ptr != b->buf) free(b->ptr);
}
static fl_str fl_sb_take(fl_sb* b) {
    char* p = (char*)fl_alloc(b->len + 1);
    memcpy(p, b->ptr, (size_t)b->len);
    p[b->len] = 0;
    if (b->ptr != b->buf) free(b->ptr);
    return (fl_str){p, b->len};
}

/* ---------- strings ---------- */
static inline bool fl_str_eq(fl_str a, fl_str b) {
    return a.len == b.len && (a.ptr == b.ptr || memcmp(a.ptr, b.ptr, (size_t)a.len) == 0);
}
static int fl_str_cmp(fl_str a, fl_str b) {
    int64_t n = a.len < b.len ? a.len : b.len;
    int c = n ? memcmp(a.ptr, b.ptr, (size_t)n) : 0;
    if (c) return c;
    return a.len < b.len ? -1 : a.len > b.len;
}
static fl_str fl_str_concat(fl_str a, fl_str b) {
    char* p = (char*)fl_alloc(a.len + b.len + 1);
    memcpy(p, a.ptr, (size_t)a.len);
    memcpy(p + a.len, b.ptr, (size_t)b.len);
    return (fl_str){p, a.len + b.len};
}
static int64_t fl_str_find(fl_str s, fl_str sub) {
    if (sub.len == 0) return 0;
    for (int64_t i = 0; i + sub.len <= s.len; i++)
        if (s.ptr[i] == sub.ptr[0] && memcmp(s.ptr + i, sub.ptr, (size_t)sub.len) == 0) return i;
    return -1;
}
static inline bool fl_str_contains(fl_str s, fl_str sub) { return fl_str_find(s, sub) >= 0; }
static inline bool fl_str_starts_with(fl_str s, fl_str p) { return s.len >= p.len && memcmp(s.ptr, p.ptr, (size_t)p.len) == 0; }
static inline bool fl_str_ends_with(fl_str s, fl_str p) { return s.len >= p.len && memcmp(s.ptr + s.len - p.len, p.ptr, (size_t)p.len) == 0; }
static fl_str fl_str_trim(fl_str s) {
    while (s.len && (s.ptr[0] == ' ' || s.ptr[0] == '\t' || s.ptr[0] == '\n' || s.ptr[0] == '\r')) { s.ptr++; s.len--; }
    while (s.len && (s.ptr[s.len-1] == ' ' || s.ptr[s.len-1] == '\t' || s.ptr[s.len-1] == '\n' || s.ptr[s.len-1] == '\r')) s.len--;
    return s;
}
static fl_str fl_str_clone(fl_str s) {
    char* p = (char*)fl_alloc(s.len + 1);
    memcpy(p, s.ptr, (size_t)s.len);
    return (fl_str){p, s.len};
}
static inline char* fl_str_cstr(fl_str s) { return fl_str_clone(s).ptr; }
static fl_list_str fl_str_split(fl_str s, fl_str sep) {
    fl_list_str out = {0};
    if (sep.len == 0) { fl_list_str_push(&out, s); return out; }
    int64_t start = 0;
    for (int64_t i = 0; i + sep.len <= s.len; ) {
        if (memcmp(s.ptr + i, sep.ptr, (size_t)sep.len) == 0) {
            fl_list_str_push(&out, (fl_str){s.ptr + start, i - start});
            i += sep.len; start = i;
        } else i++;
    }
    fl_list_str_push(&out, (fl_str){s.ptr + start, s.len - start});
    return out;
}

/* ---------- I/O and misc ---------- */
static fl_slice_str fl_args;
static void fl_init_args(int argc, char** argv) {
    fl_args.ptr = (fl_str*)fl_alloc(argc * (int64_t)sizeof(fl_str));
    fl_args.len = argc;
    for (int i = 0; i < argc; i++) fl_args.ptr[i] = (fl_str){argv[i], (int64_t)strlen(argv[i])};
}
static fl_str fl_read_line(void) {
    fl_sb b; fl_sb_init(&b);
    int c, any = 0;
    while ((c = getchar()) != EOF) {
        any = 1;
        if (c == '\n') break;
        fl_sb_char(&b, (char)c);
    }
    if (!any) { if (b.ptr != b.buf) free(b.ptr); return (fl_str){NULL, 0}; }
    if (b.len && b.ptr[b.len - 1] == '\r') b.len--;
    return fl_sb_take(&b);
}
static int64_t fl_parse_int(fl_str s) {
    char buf[64];
    s = fl_str_trim(s);
    int64_t n = s.len < 63 ? s.len : 63;
    memcpy(buf, s.ptr, (size_t)n); buf[n] = 0;
    return (int64_t)strtoll(buf, NULL, 0);
}
static double fl_parse_float(fl_str s) {
    char buf[128];
    s = fl_str_trim(s);
    int64_t n = s.len < 127 ? s.len : 127;
    memcpy(buf, s.ptr, (size_t)n); buf[n] = 0;
    return strtod(buf, NULL);
}
static int64_t fl_time_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}
static fl_str fl_read_file(fl_str path) {
    char* p = fl_str_cstr(path);
    FILE* f = fopen(p, "rb");
    free(p);
    if (!f) return (fl_str){NULL, 0};
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char* data = (char*)fl_alloc(n + 1);
    size_t got = fread(data, 1, (size_t)n, f);
    fclose(f);
    return (fl_str){data, (int64_t)got};
}
static bool fl_write_file(fl_str path, fl_str data) {
    char* p = fl_str_cstr(path);
    FILE* f = fopen(p, "wb");
    free(p);
    if (!f) return false;
    size_t w = fwrite(data.ptr, 1, (size_t)data.len, f);
    fclose(f);
    return w == (size_t)data.len;
}
/* ===== end of Flint runtime ===== */
