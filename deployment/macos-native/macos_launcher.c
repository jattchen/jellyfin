/* Named macOS app process for the native Jellyfin server.
 * Waits until the data volume and the media volume behind /media are mounted,
 * then replaces itself with the official Jellyfin executable.
 * Paths are injected at build time by jellyfin_native.py.
 */
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <syslog.h>
#include <unistd.h>

#ifndef DATA_VOLUME
#error "DATA_VOLUME must be defined"
#endif

static int mounted_at(const char *path, const char *mount_point) {
    struct statfs fs;
    return statfs(path, &fs) == 0 && strcmp(fs.f_mntonname, mount_point) == 0;
}

/* /media resolves through a synthetic link; it must land on a mounted volume,
 * never on the system or data volume, or a library scan would see an empty tree. */
static int media_ready(void) {
    char resolved[PATH_MAX];
    struct statfs fs;
    if (!realpath("/media", resolved) || statfs(resolved, &fs) != 0) return 0;
    return strcmp(fs.f_mntonname, "/") != 0 && strcmp(fs.f_mntonname, "/System/Volumes/Data") != 0;
}

static const char *blocker(void) {
    if (!mounted_at(DATA_VOLUME, DATA_VOLUME)) return "data volume not mounted";
    if (access("/config/config/system.xml", R_OK) != 0) return "/config/config/system.xml unreadable";
    if (!media_ready()) return "/media not on a mounted media volume";
    return NULL;
}

int main(void) {
    const char *reported = NULL;
    const char *reason;
    openlog("Jellyfin Server", LOG_PID, LOG_USER);
    while ((reason = blocker()) != NULL) {
        if (reason != reported) {
            syslog(LOG_ERR, "Waiting: %s (%s)", reason, strerror(errno));
            fprintf(stderr, "Waiting: %s (%s)\n", reason, strerror(errno));
            reported = reason;
        }
        sleep(10);
    }
    char *argv[] = {
        SERVER_EXE,
        "--datadir", "/config",
        "--configdir", "/config/config",
        "--logdir", "/config/log",
        "--cachedir", CACHE_DIR,
        "--webdir", WEB_DIR,
        "--ffmpeg", FFMPEG_EXE,
        "--service",
        NULL,
    };
    execv(SERVER_EXE, argv);
    syslog(LOG_ERR, "Unable to start %s", SERVER_EXE);
    return 1;
}
