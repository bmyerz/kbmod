NAME=skyToIdx

WCS_INSTALL=/opt/local
POSTGRESQL_HEADERS=/opt/local/include/postgresql93/server

CC=cc
CFLAGS=-fpic -I${WCS_INSTALL}/include -I${POSTGRESQL_HEADERS}
BUNDLE_FLAGS=-bundle -flat_namespace -undefined suppress -lwcs -L${WCS_INSTALL}/lib


${NAME}.o: skyToIdx.c
	${CC} ${CFLAGS} -c $<

${NAME}.so:	${NAME}.o
	${CC} ${BUNDLE_FLAGS} -o $@ $<

clean:
	rm skyToIdx.o skyToIdx.so 
