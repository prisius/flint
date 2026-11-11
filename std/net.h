/* std/net.h: minimal TCP helpers for std/net.fl (POSIX sockets).
 * Kept in C because socket address structs differ between Linux and macOS. */
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>

/* Listen on 0.0.0.0:port. Returns a socket fd, or -1 on error. */
static int net_listen(int port) {
    signal(SIGPIPE, SIG_IGN);  /* a client hanging up must not kill the server */
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(fd, (struct sockaddr*)&addr, sizeof addr) < 0 || listen(fd, 128) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

/* Wait for a client. Returns a connection fd, or -1. */
static int net_accept(int fd) {
    for (;;) {
        int c = accept(fd, NULL, NULL);
        if (c >= 0 || errno != EINTR) return c;
    }
}

/* Read up to cap bytes. Returns bytes read, 0 on close, -1 on error. */
static int64_t net_read(int fd, char* buf, int64_t cap) {
    for (;;) {
        ssize_t n = recv(fd, buf, (size_t)cap, 0);
        if (n >= 0 || errno != EINTR) return (int64_t)n;
    }
}

/* Write all bytes. Returns bytes written, or -1 on error. */
static int64_t net_write(int fd, const char* data, int64_t len) {
    int64_t done = 0;
    while (done < len) {
        ssize_t n = send(fd, data + done, (size_t)(len - done), 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        done += n;
    }
    return done;
}

static void net_close(int fd) { close(fd); }
