/**
 * Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.
 * @file h102_ft601_persistent_dma.c
 * @brief Persistent DMA buffer-lifetime adapter for pinned finite FT601 mixed transport.
 * @details Active transfers lease dev_mem cached by handle/length and restore SDK objects before completion; preserve status/data protocol.
 *          Allocation failure never falls back to malloc; active free/close aborts to prevent dangling DMA.
 * @author LogicRF Technologies
 * @architect LogicRF
 * @email info@logicrf.com
 * @website https://www.logicrf.com
 * @version 0.1.0
 * @date 2026-10-11
 * @note Linked into candidate programs only; outer digital guard handles board recovery after exceptions.
 */
#define _GNU_SOURCE
#include "h102_ft601_persistent_dma.h"
#include "libusb.h"
#include <dlfcn.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define DMA_SLOTS 1024u ///< Fixed cap; reject unbounded growth; actual slot counts require SDK/board evidence.
/** @brief SDK owns original buffers; handle cache retains DMA mappings after transfer release until close. */
struct dma_slot {
    struct libusb_transfer *transfer;
    libusb_device_handle *handle;
    unsigned char *dma,*original;
    libusb_transfer_cb_fn callback;
    size_t size;
    uint8_t flags;
    bool active;
};
static struct dma_slot slots[DMA_SLOTS];
static struct h102_dma_stats stats;
static pthread_mutex_t mutex=PTHREAD_MUTEX_INITIALIZER;
static pthread_once_t once=PTHREAD_ONCE_INIT;
static int initialized=LIBUSB_ERROR_NOT_SUPPORTED;
static bool quiescing;
static unsigned callbacks; ///< Even with active=0, pending original SDK callbacks prevent handle closure.
static int (*real_submit)(struct libusb_transfer *);
static int (*real_cancel)(struct libusb_transfer *);
static void (*real_free)(struct libusb_transfer *);
static void (*real_close)(libusb_device_handle *);
static unsigned char *(*real_alloc)(libusb_device_handle *,size_t);
static int (*real_release)(libusb_device_handle *,unsigned char *,size_t);
static libusb_device *(*real_device)(libusb_device_handle *);
static int (*real_descriptor)(libusb_device *,struct libusb_device_descriptor *);

/** @brief Bind actual SDK functions with RTLD_NEXT to avoid wrapper recursion, without opening devices. */
static void resolve(void)
{
    real_submit=dlsym(RTLD_NEXT,"libusb_submit_transfer");
    real_cancel=dlsym(RTLD_NEXT,"libusb_cancel_transfer");
    real_free=dlsym(RTLD_NEXT,"libusb_free_transfer");
    real_close=dlsym(RTLD_NEXT,"libusb_close");
    real_alloc=dlsym(RTLD_NEXT,"libusb_dev_mem_alloc");
    real_release=dlsym(RTLD_NEXT,"libusb_dev_mem_free");
    real_device=dlsym(RTLD_NEXT,"libusb_get_device");
    real_descriptor=dlsym(RTLD_NEXT,"libusb_get_device_descriptor");
    const struct libusb_version *(*version)(void)=dlsym(RTLD_NEXT,"libusb_get_version");
    if(!real_submit || !real_cancel || !real_free || !real_close || !real_alloc || !real_release ||
            !real_device || !real_descriptor || !version)return;
    const struct libusb_version *v=version();
    if(v && v->major==1 && v->minor==0 && v->micro==27 && v->nano==11882)initialized=0;
}
int h102_dma_initialize(void){pthread_once(&once,resolve);return initialized;}
struct h102_dma_stats h102_dma_snapshot(void)
{
    pthread_mutex_lock(&mutex);struct h102_dma_stats s=stats;pthread_mutex_unlock(&mutex);return s;
}
void h102_dma_report(FILE *output)
{
    struct h102_dma_stats s=h102_dma_snapshot();
    fprintf(output,"{\"allocated\":%"PRIu64",\"released\":%"PRIu64",\"submitted\":%"PRIu64
        ",\"cache_hits\":%"PRIu64",\"completed\":%"PRIu64",\"errors\":%"PRIu64",\"bytes\":%"PRIu64
        ",\"peak_bytes\":%"PRIu64",\"active\":%"PRIu64",\"peak_active\":%"PRIu64"}\n",
        s.allocated,s.released,s.submitted,s.cache_hits,s.completed,s.errors,s.bytes,s.peak_bytes,s.active,s.peak_active);
}
static struct dma_slot *find(struct libusb_transfer *t)
{
    for(unsigned i=0;i<DMA_SLOTS;++i)if(slots[i].transfer==t)return &slots[i];
    return NULL;
}
/** @brief Called with mutex held; abort failed unmaps rather than reclaim active DMA. */
static void release_slot(struct dma_slot *s)
{
    if(s->active){
        fprintf(stderr,"H102_DMA_FATAL reason=active_close active=%"PRIu64" size=%zu\n",stats.active,s->size);
        fflush(stderr);++stats.errors;abort();
    }
    int result=real_release(s->handle,s->dma,s->size);
    if(result!=0){
        fprintf(stderr,"H102_DMA_FATAL reason=devmem_free result=%d size=%zu\n",result,s->size);
        fflush(stderr);++stats.errors;abort();
    }
    stats.bytes-=s->size;++stats.released;memset(s,0,sizeof(*s));
}
static int target(struct libusb_transfer *t)
{
    if(!t || t->type!=LIBUSB_TRANSFER_TYPE_BULK ||
       !((t->endpoint==0x82 && t->length==65536) || (t->endpoint==0x02 && t->length==66560)))return 0;
    struct libusb_device_descriptor d;
    if(real_descriptor(real_device(t->dev_handle),&d)!=0)return LIBUSB_ERROR_IO;
    return d.idVendor==0x0403 && d.idProduct==0x601f;
}
static void restore(struct dma_slot *s)
{
    struct libusb_transfer *t=s->transfer;
    t->buffer=s->original;t->callback=s->callback;t->flags=s->flags;s->active=false;--stats.active;
}
/** @brief Callbacks may free/resubmit transfers; never access t/s after the original callback. */
static void completion(struct libusb_transfer *t)
{
    pthread_mutex_lock(&mutex);struct dma_slot *s=find(t);
    if(!s || !s->active){++stats.errors;abort();}
    libusb_transfer_cb_fn callback=s->callback;
    if(t->actual_length<0 || (size_t)t->actual_length>s->size){
        ++stats.errors;t->status=LIBUSB_TRANSFER_ERROR;t->actual_length=0;
    }else if(t->endpoint&LIBUSB_ENDPOINT_IN)memcpy(s->original,s->dma,(size_t)t->actual_length);
    ++stats.completed;++callbacks;restore(s);pthread_mutex_unlock(&mutex);
    callback(t);
    pthread_mutex_lock(&mutex);--callbacks;pthread_mutex_unlock(&mutex);
}
int h102_dma_quiesce(unsigned milliseconds)
{
    if(h102_dma_initialize()!=0)return LIBUSB_ERROR_NOT_SUPPORTED;
    struct timespec current;clock_gettime(CLOCK_MONOTONIC,&current);
    double deadline=current.tv_sec+current.tv_nsec*1e-9+milliseconds*1e-3;
    pthread_mutex_lock(&mutex);quiescing=true;
    int result=0;
    for(unsigned i=0;i<DMA_SLOTS;++i)if(slots[i].active){
        // libusb cancellation is asynchronous; hold the lock against callback frees, then let SDK event threads handle completion.
        int status=real_cancel(slots[i].transfer);
        if(status!=0 && status!=LIBUSB_ERROR_NOT_FOUND){++stats.errors;result=status;}
    }
    pthread_mutex_unlock(&mutex);
    for(;;){
        pthread_mutex_lock(&mutex);bool idle=stats.active==0 && callbacks==0;pthread_mutex_unlock(&mutex);
        if(idle)return result;
        clock_gettime(CLOCK_MONOTONIC,&current);
        if(current.tv_sec+current.tv_nsec*1e-9>=deadline)return LIBUSB_ERROR_TIMEOUT;
        struct timespec pause={.tv_nsec=1000000};nanosleep(&pause,NULL);
    }
}
int LIBUSB_CALL libusb_submit_transfer(struct libusb_transfer *t)
{
    if(h102_dma_initialize()!=0)return LIBUSB_ERROR_NOT_SUPPORTED;
    pthread_mutex_lock(&mutex);struct dma_slot *s=find(t);
    if(s && s->active){pthread_mutex_unlock(&mutex);return LIBUSB_ERROR_BUSY;}
    int scope=target(t);
    if(scope<0){++stats.errors;pthread_mutex_unlock(&mutex);return scope;}
    if(!scope){pthread_mutex_unlock(&mutex);return real_submit(t);}
    if(quiescing){pthread_mutex_unlock(&mutex);return LIBUSB_ERROR_BUSY;}
    if(!t->callback || !t->buffer || (t->flags&LIBUSB_TRANSFER_FREE_TRANSFER)){
        ++stats.errors;pthread_mutex_unlock(&mutex);return LIBUSB_ERROR_NOT_SUPPORTED;
    }
    if(s && (s->handle!=t->dev_handle || s->size!=(size_t)t->length))release_slot(s),s=NULL;
    if(!s){
        // SDK creates transfers per request; prefer unleased mappings with the same handle/length.
        for(unsigned i=0;i<DMA_SLOTS;++i)if(!slots[i].transfer && slots[i].dma &&
            slots[i].handle==t->dev_handle && slots[i].size==(size_t)t->length){s=&slots[i];break;}
        if(s){s->transfer=t;++stats.cache_hits;}
        else {
            for(unsigned i=0;i<DMA_SLOTS;++i)if(!slots[i].dma){s=&slots[i];break;}
            unsigned char *dma=s?real_alloc(t->dev_handle,(size_t)t->length):NULL;
            if(!dma){++stats.errors;pthread_mutex_unlock(&mutex);return LIBUSB_ERROR_NO_MEM;}
            *s=(struct dma_slot){.transfer=t,.handle=t->dev_handle,.dma=dma,.size=(size_t)t->length};
            ++stats.allocated;stats.bytes+=s->size;
            if(stats.bytes>stats.peak_bytes)stats.peak_bytes=stats.bytes;
        }
    }
    s->original=t->buffer;s->callback=t->callback;s->flags=t->flags;s->active=true;
    ++stats.active;if(stats.active>stats.peak_active)stats.peak_active=stats.active;++stats.submitted;
    if(!(t->endpoint&LIBUSB_ENDPOINT_IN))memcpy(s->dma,s->original,s->size);
    t->buffer=s->dma;t->callback=completion;t->flags&=~LIBUSB_TRANSFER_FREE_BUFFER;
    pthread_mutex_unlock(&mutex);
    int result=real_submit(t);
    // Successful callbacks may already free/resubmit objects; never access them on return-zero paths.
    if(result!=0){pthread_mutex_lock(&mutex);s=find(t);if(!s || !s->active)abort();
        restore(s);++stats.errors;pthread_mutex_unlock(&mutex);}
    return result;
}
void LIBUSB_CALL libusb_free_transfer(struct libusb_transfer *t)
{
    if(h102_dma_initialize()!=0)abort();
    pthread_mutex_lock(&mutex);struct dma_slot *s=find(t);
    if(s){
        if(s->active){
            fprintf(stderr,"H102_DMA_FATAL reason=active_transfer_free active=%"PRIu64" endpoint=%u size=%zu\n",
                stats.active,(unsigned)t->endpoint,s->size);
            fflush(stderr);++stats.errors;abort();
        }
        s->transfer=NULL;s->original=NULL;s->callback=NULL;s->flags=0;
    }
    pthread_mutex_unlock(&mutex);real_free(t);
}
void LIBUSB_CALL libusb_close(libusb_device_handle *handle)
{
    if(h102_dma_initialize()!=0)abort();
    pthread_mutex_lock(&mutex);
    for(unsigned i=0;i<DMA_SLOTS;++i)if(slots[i].dma && slots[i].handle==handle)release_slot(&slots[i]);
    pthread_mutex_unlock(&mutex);real_close(handle);
}
