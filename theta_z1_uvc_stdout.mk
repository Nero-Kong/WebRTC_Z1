PKG_CONFIG_LIB = gstreamer-app-1.0 libuvc

CFLAGS += -O2 -Wall -Wextra $(shell pkg-config --cflags $(PKG_CONFIG_LIB))
LDFLAGS += $(shell pkg-config --libs $(PKG_CONFIG_LIB))

SRCS = theta_z1_uvc_stdout.c thetauvc.c
OBJS := $(SRCS:.c=.o)

all: theta_z1_uvc_stdout

theta_z1_uvc_stdout: $(OBJS)
	$(CC) $(OBJS) -o $@ $(LDFLAGS) -pthread

clean:
	$(RM) $(OBJS) theta_z1_uvc_stdout
