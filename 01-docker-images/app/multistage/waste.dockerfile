FROM alpine
RUN dd if=/dev/zero of=/big bs=1M count=50
RUN rm /big
