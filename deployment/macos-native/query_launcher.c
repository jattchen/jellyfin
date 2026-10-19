/* Named app wrapper: retain macOS volume permission and wait for Turbo. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <unistd.h>

int main(void) {
    struct statfs fs;
    int reported = 0;
    while (statfs("/Volumes/Turbo", &fs) != 0 || strcmp(fs.f_mntonname, "/Volumes/Turbo") != 0
           || access(QUERY_PYTHON, X_OK) != 0 || access(QUERY_SCRIPT, R_OK) != 0) {
        if (!reported) { fprintf(stderr, "Jellyfin Search Model: waiting for Turbo and runtime\n"); reported = 1; }
        sleep(10);
    }
    setenv("EMBEDDING_HOME", QUERY_HOME, 1);
    setenv("PYTHONPATH", QUERY_SOURCE, 1);
    setenv("HF_HUB_OFFLINE", "1", 1);
    setenv("HF_HUB_DISABLE_TELEMETRY", "1", 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    char *argv[] = { QUERY_PYTHON, QUERY_SCRIPT, "serve", NULL };
    execv(QUERY_PYTHON, argv);
    perror("Jellyfin Search Model: exec failed");
    return 1;
}
