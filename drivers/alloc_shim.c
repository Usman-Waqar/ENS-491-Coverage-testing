/* Plain pass-through allocator linked into drivers that do not define their
   own ini_malloc/ini_free/ini_realloc, so every driver can be linked when a
   configuration sets INI_CUSTOM_ALLOCATOR=1 (and INI_USE_STACK=0). */
#include <stdlib.h>

void* ini_malloc(size_t size) { return malloc(size); }
void ini_free(void* ptr) { free(ptr); }
void* ini_realloc(void* ptr, size_t size) { return realloc(ptr, size); }
