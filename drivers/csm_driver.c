/* csm_driver -- runtime-configurable test driver for inih (generated for the CIT study)

   Re-hosts the developer test inputs of tests/unittest.c (plus generated inputs)
   and makes the two runtime options of the CSM selectable without a rebuild:

     --api      ini_parse | ini_parse_file | ini_parse_stream |
                ini_parse_string | ini_parse_string_length
     --handler  reject_flagged | accept_all | reject_all

   Output format on stdout is byte-identical to tests/unittest.c's dumper, so
   the developer golden baselines (tests/baseline_*.txt) remain usable as an
   oracle when --handler reject_flagged is selected.

   A second, configuration-independent oracle runs on every input: the same
   bytes are parsed again through a reference entry point and the callback
   trace + return code must be identical. One line per input is written to
   stderr:  DIFF_ORACLE <file> PASS|FAIL|SKIP [details]

   For entry points that do not open the file themselves (string/stream/file
   APIs), a missing input file is reported as e=-1, mirroring ini_parse(). */

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ini.h"

typedef struct { char* buf; size_t len; size_t cap; } sbuf;

static void sb_reset(sbuf* b) { b->len = 0; if (b->buf) b->buf[0] = '\0'; }

static void sb_printf(sbuf* b, const char* fmt, ...)
{
    va_list ap;
    int n;
    va_start(ap, fmt);
    n = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if (b->len + (size_t)n + 1 > b->cap) {
        size_t cap = b->cap ? b->cap : 256;
        while (b->len + (size_t)n + 1 > cap) cap *= 2;
        b->buf = (char*)realloc(b->buf, cap);
        if (!b->buf) { fputs("out of memory\n", stderr); exit(2); }
        b->cap = cap;
    }
    va_start(ap, fmt);
    vsnprintf(b->buf + b->len, (size_t)n + 1, fmt, ap);
    va_end(ap);
    b->len += (size_t)n;
}

enum policy { REJECT_FLAGGED, ACCEPT_ALL, REJECT_ALL };
enum api { API_PARSE, API_FILE, API_STREAM, API_STRING, API_STRING_LENGTH, API_COUNT };
static const char* API_NAMES[API_COUNT] = {
    "ini_parse", "ini_parse_file", "ini_parse_stream",
    "ini_parse_string", "ini_parse_string_length"
};

static int User;
static char Prev_section[50];   /* same size as tests/unittest.c */
static sbuf* Out;
static enum policy Policy = REJECT_FLAGGED;

/* Mirrors dumper() in tests/unittest.c, writing to Out instead of stdout. */
#if INI_HANDLER_LINENO
static int dumper(void* user, const char* section, const char* name,
                  const char* value, int lineno)
#else
static int dumper(void* user, const char* section, const char* name,
                  const char* value)
#endif
{
    User = *((int*)user);
    if (!name || strcmp(section, Prev_section)) {
        sb_printf(Out, "... [%s]\n", section);
        strncpy(Prev_section, section, sizeof(Prev_section));
        Prev_section[sizeof(Prev_section) - 1] = '\0';
    }
    if (!name)
        return Policy == REJECT_ALL ? 0 : 1;
#if INI_HANDLER_LINENO
    sb_printf(Out, "... %s%s%s;  line %d\n", name, value ? "=" : "", value ? value : "", lineno);
#else
    sb_printf(Out, "... %s%s%s;\n", name, value ? "=" : "", value ? value : "");
#endif
    if (Policy == REJECT_ALL)
        return 0;
    if (!value || Policy == ACCEPT_ALL)
        return 1;
    return strcmp(name, "user") == 0 && strcmp(value, "parse_error") == 0 ? 0 : 1;
}

static char* fgets_reader(char* str, int num, void* stream)
{
    return fgets(str, num, (FILE*)stream);
}

static char* read_all(const char* fname, size_t* len)
{
    FILE* f = fopen(fname, "rb");
    char* data;
    long n;
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0 || (n = ftell(f)) < 0) { fclose(f); return NULL; }
    rewind(f);
    data = (char*)malloc((size_t)n + 1);
    if (!data) { fclose(f); return NULL; }
    *len = fread(data, 1, (size_t)n, f);
    data[*len] = '\0';
    fclose(f);
    return data;
}

static int run_api(enum api a, const char* fname, int* user)
{
    FILE* f;
    char* data;
    size_t len = 0;
    int e;

    switch (a) {
    case API_PARSE:
        return ini_parse(fname, dumper, user);
    case API_FILE:
    case API_STREAM:
        f = fopen(fname, "r");
        if (!f) return -1;
        e = (a == API_FILE) ? ini_parse_file(f, dumper, user)
                            : ini_parse_stream(fgets_reader, f, dumper, user);
        fclose(f);
        return e;
    case API_STRING:
    case API_STRING_LENGTH:
        data = read_all(fname, &len);
        if (!data) return -1;
        e = (a == API_STRING) ? ini_parse_string(data, dumper, user)
                              : ini_parse_string_length(data, len, dumper, user);
        free(data);
        return e;
    default:
        return -100;
    }
}

static int lookup(const char* s, const char* const* names, int n)
{
    int i;
    for (i = 0; i < n; i++)
        if (strcmp(s, names[i]) == 0) return i;
    return -1;
}

int main(int argc, char** argv)
{
    static const char* const POLICY_NAMES[] = { "reject_flagged", "accept_all", "reject_all" };
    enum api api = API_PARSE, ref;
    sbuf a = {0}, b = {0};
    int u = 100, i, e, e_ref, saved_user, mismatches = 0;
    FILE* probe;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--api") == 0 && i + 1 < argc) {
            int k = lookup(argv[++i], API_NAMES, API_COUNT);
            if (k < 0) { fprintf(stderr, "unknown --api %s\n", argv[i]); return 2; }
            api = (enum api)k;
        } else if (strcmp(argv[i], "--handler") == 0 && i + 1 < argc) {
            int k = lookup(argv[++i], POLICY_NAMES, 3);
            if (k < 0) { fprintf(stderr, "unknown --handler %s\n", argv[i]); return 2; }
            Policy = (enum policy)k;
        } else if (strcmp(argv[i], "--") == 0) {
            i++;
            break;
        } else {
            break;
        }
    }
    ref = (api == API_STRING_LENGTH) ? API_PARSE : API_STRING_LENGTH;

    for (; i < argc; i++) {
        const char* fname = argv[i];

        Out = &a; sb_reset(&a);
        *Prev_section = '\0';
        e = run_api(api, fname, &u);
        saved_user = User;
        fputs(a.buf ? a.buf : "", stdout);
        printf("%s: e=%d user=%d\n", fname, e, User);
        fflush(stdout);

        probe = fopen(fname, "rb");
        if (!probe) {
            fprintf(stderr, "DIFF_ORACLE %s SKIP missing-input\n", fname);
        } else {
            fclose(probe);
            Out = &b; sb_reset(&b);
            *Prev_section = '\0';
            e_ref = run_api(ref, fname, &u);
            User = saved_user;
            if (e != e_ref || a.len != b.len || (a.len && memcmp(a.buf, b.buf, a.len) != 0)) {
                fprintf(stderr, "DIFF_ORACLE %s FAIL api=%s e=%d ref=%s e=%d\n",
                        fname, API_NAMES[api], e, API_NAMES[ref], e_ref);
                mismatches++;
            } else {
                fprintf(stderr, "DIFF_ORACLE %s PASS\n", fname);
            }
        }
        u++;
    }
    free(a.buf);
    free(b.buf);
    return mismatches ? 1 : 0;
}
