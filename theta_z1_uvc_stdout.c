/*
 * RICOH THETA Z1 libuvc-theta stdout bridge.
 *
 * This reads the Z1 USB live-streaming H.264 stream through RICOH's
 * libuvc-theta branch and writes the elementary stream to stdout.
 */

#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "libuvc/libuvc.h"
#include "thetauvc.h"

static volatile sig_atomic_t g_running = 1;

static void handle_signal(int signum)
{
    (void)signum;
    g_running = 0;
}

static int write_all(const uint8_t *data, size_t size)
{
    size_t offset = 0;
    while (offset < size) {
        ssize_t written = write(STDOUT_FILENO, data + offset, size - offset);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (written == 0) {
            return -1;
        }
        offset += (size_t)written;
    }
    return 0;
}

static void frame_callback(uvc_frame_t *frame, void *ptr)
{
    (void)ptr;
    if (!g_running || !frame || !frame->data || frame->data_bytes == 0) {
        return;
    }

    if (write_all((const uint8_t *)frame->data, frame->data_bytes) != 0) {
        g_running = 0;
    }
}

static void print_usage(const char *argv0)
{
    fprintf(stderr,
        "Usage: %s [--mode z1-4k|z1-2k] [--list]\\n"
        "  z1-4k: 3840x1920 29.97fps H.264\\n"
        "  z1-2k: 1920x960 29.97fps H.264\\n",
        argv0);
}

int main(int argc, char **argv)
{
    unsigned int mode = THETAUVC_MODE_UHD_2997;
    int list_only = 0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--mode") == 0 && i + 1 < argc) {
            i++;
            if (strcmp(argv[i], "z1-4k") == 0) {
                mode = THETAUVC_MODE_UHD_2997;
            } else if (strcmp(argv[i], "z1-2k") == 0) {
                mode = THETAUVC_MODE_FHD_2997;
            } else {
                print_usage(argv[0]);
                return 2;
            }
        } else if (strcmp(argv[i], "--list") == 0) {
            list_only = 1;
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            print_usage(argv[0]);
            return 0;
        } else {
            print_usage(argv[0]);
            return 2;
        }
    }

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);
    signal(SIGPIPE, handle_signal);

    uvc_context_t *ctx = NULL;
    uvc_device_t *dev = NULL;
    uvc_device_handle_t *devh = NULL;
    uvc_stream_ctrl_t ctrl;
    uvc_error_t res;

    res = uvc_init(&ctx, NULL);
    if (res != UVC_SUCCESS) {
        uvc_perror(res, "uvc_init");
        return (int)res;
    }

    if (list_only) {
        res = thetauvc_print_devices(ctx, stderr);
        uvc_exit(ctx);
        return res == UVC_SUCCESS ? 0 : (int)res;
    }

    res = thetauvc_find_device(ctx, &dev, 0);
    if (res != UVC_SUCCESS) {
        fprintf(stderr, "THETA Z1 UVC device not found. Is the camera in Live Streaming/UVC mode?\\n");
        uvc_exit(ctx);
        return (int)res;
    }

    res = uvc_open(dev, &devh);
    if (res != UVC_SUCCESS) {
        uvc_perror(res, "uvc_open");
        uvc_exit(ctx);
        return (int)res;
    }

    res = thetauvc_get_stream_ctrl_format_size(devh, mode, &ctrl);
    if (res != UVC_SUCCESS) {
        uvc_perror(res, "thetauvc_get_stream_ctrl_format_size");
        uvc_close(devh);
        uvc_exit(ctx);
        return (int)res;
    }

    fprintf(stderr,
        "[theta-z1-uvc] starting mode=%s dwFrameInterval=%u dwClockFrequency=%u\\n",
        mode == THETAUVC_MODE_UHD_2997 ? "z1-4k" : "z1-2k",
        ctrl.dwFrameInterval,
        ctrl.dwClockFrequency);

    res = uvc_start_streaming(devh, &ctrl, frame_callback, NULL, 0);
    if (res != UVC_SUCCESS) {
        uvc_perror(res, "uvc_start_streaming");
        uvc_close(devh);
        uvc_exit(ctx);
        return (int)res;
    }

    while (g_running) {
        usleep(100000);
    }

    fprintf(stderr, "[theta-z1-uvc] stopping\\n");
    uvc_stop_streaming(devh);
    uvc_close(devh);
    uvc_exit(ctx);
    return 0;
}
