// XDP BPF program for WaddleAI proxy server
// Provides: DDoS protection, per-IP rate limiting, connection tracking
//
// Compilation:
//   clang -O2 -target bpf -c xdp_filter.c -o xdp_filter.o
//
// Requirements:
//   - Linux kernel 5.10+
//   - clang with BPF target support
//   - kernel headers installed

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/in.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

// BPF Maps for state and configuration

// Map: rate_limits
// Stores per-IP rate limits (requests per second)
// Key: __u32 (IP address in network byte order)
// Value: __u32 (rate limit in requests/second)
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 10000);
    __type(key, __u32);    // Source IP address
    __type(value, __u32);  // Rate limit (requests per second)
} rate_limits SEC(".maps");

// Map: rate_state
// Tracks current rate limit state for each IP
// Key: __u32 (IP address)
// Value: __u64 (packed: upper 32 bits = last timestamp, lower 32 bits = counter)
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 10000);
    __type(key, __u32);
    __type(value, __u64);
} rate_state SEC(".maps");

// Map: stats
// Global statistics counters
// Key: __u32 (stat ID)
// Value: __u64 (counter)
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 10);
    __type(key, __u32);
    __type(value, __u64);
} stats SEC(".maps");

// Stat IDs
#define STAT_PACKETS_TOTAL 0
#define STAT_PACKETS_DROPPED 1
#define STAT_PACKETS_RATE_LIMITED 2
#define STAT_PACKETS_PASSED 3
#define STAT_NON_TCP 4
#define STAT_INVALID_PACKETS 5

// Helper: Update statistics counter atomically
static __always_inline void update_stat(__u32 stat_id) {
    __u64 *value = bpf_map_lookup_elem(&stats, &stat_id);
    if (value) {
        __sync_fetch_and_add(value, 1);
    }
}

// Main XDP program
// Processes incoming packets and applies rate limiting
SEC("xdp")
int waddleai_filter(struct xdp_md *ctx) {
    void *data_end = (void *)(long)ctx->data_end;
    void *data = (void *)(long)ctx->data;

    // Update total packet counter
    update_stat(STAT_PACKETS_TOTAL);

    // Parse Ethernet header
    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end) {
        update_stat(STAT_INVALID_PACKETS);
        return XDP_DROP;
    }

    // Only process IPv4 packets (skip IPv6, ARP, etc.)
    if (eth->h_proto != bpf_htons(ETH_P_IP)) {
        return XDP_PASS;  // Pass non-IP packets to kernel
    }

    // Parse IP header
    struct iphdr *ip = data + sizeof(*eth);
    if ((void *)(ip + 1) > data_end) {
        update_stat(STAT_INVALID_PACKETS);
        return XDP_DROP;
    }

    // Only process TCP packets (HTTP/HTTPS/WebSocket)
    if (ip->protocol != IPPROTO_TCP) {
        update_stat(STAT_NON_TCP);
        return XDP_PASS;  // Pass non-TCP to kernel (UDP, ICMP, etc.)
    }

    // Parse TCP header (optional, for future enhancements)
    struct tcphdr *tcp = (void *)ip + sizeof(*ip);
    if ((void *)(tcp + 1) > data_end) {
        update_stat(STAT_INVALID_PACKETS);
        return XDP_DROP;
    }

    // Extract source IP
    __u32 src_ip = ip->saddr;

    // Check if there's a rate limit configured for this IP
    __u32 *rate_limit = bpf_map_lookup_elem(&rate_limits, &src_ip);

    if (rate_limit && *rate_limit > 0) {
        // Rate limit is configured for this IP

        // Get current state for this IP
        __u64 *state = bpf_map_lookup_elem(&rate_state, &src_ip);
        __u64 now = bpf_ktime_get_ns();  // Current time in nanoseconds

        if (state) {
            // Extract last timestamp and counter from packed state
            __u64 last_time = *state >> 32;
            __u32 counter = (__u32)(*state & 0xFFFFFFFF);

            // Check if we're still within the same second
            if ((now - last_time) < 1000000000ULL) {  // 1 second in nanoseconds
                // Still within same second, check counter
                if (counter >= *rate_limit) {
                    // Rate limit exceeded - DROP packet
                    update_stat(STAT_PACKETS_RATE_LIMITED);
                    update_stat(STAT_PACKETS_DROPPED);
                    return XDP_DROP;
                }
                // Increment counter
                counter++;
            } else {
                // New second - reset counter
                counter = 1;
                last_time = now;
            }

            // Pack and update state
            __u64 new_state = (last_time << 32) | counter;
            bpf_map_update_elem(&rate_state, &src_ip, &new_state, BPF_ANY);

        } else {
            // First packet from this IP - initialize state
            __u64 new_state = (now << 32) | 1;  // timestamp in upper 32 bits, counter=1 in lower
            bpf_map_update_elem(&rate_state, &src_ip, &new_state, BPF_ANY);
        }
    }

    // Packet passed all checks - allow it through
    update_stat(STAT_PACKETS_PASSED);
    return XDP_PASS;
}

// Optional: XDP program for DDoS protection (more aggressive)
// This can be loaded on a different priority
SEC("xdp_ddos")
int waddleai_ddos_filter(struct xdp_md *ctx) {
    void *data_end = (void *)(long)ctx->data_end;
    void *data = (void *)(long)ctx->data;

    // Parse Ethernet
    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end)
        return XDP_DROP;

    // Only IPv4
    if (eth->h_proto != bpf_htons(ETH_P_IP))
        return XDP_PASS;

    // Parse IP
    struct iphdr *ip = data + sizeof(*eth);
    if ((void *)(ip + 1) > data_end)
        return XDP_DROP;

    // Check for suspicious patterns:
    // 1. Fragments (often used in attacks)
    if (ip->frag_off & bpf_htons(0x1FFF)) {
        update_stat(STAT_PACKETS_DROPPED);
        return XDP_DROP;
    }

    // 2. Invalid IP options
    if (ip->ihl > 5) {
        // Has options - could be used for attacks
        // In production, analyze options more carefully
        update_stat(STAT_PACKETS_DROPPED);
        return XDP_DROP;
    }

    // 3. Source IP is private (spoofing detection)
    __u32 src_ip = bpf_ntohl(ip->saddr);

    // Check for obviously spoofed source IPs
    // (This is simplified - production should be more sophisticated)
    if ((src_ip & 0xFF000000) == 0x00000000 ||  // 0.0.0.0/8
        (src_ip & 0xFF000000) == 0x7F000000 ||  // 127.0.0.0/8 (loopback)
        (src_ip & 0xFF000000) == 0xFF000000) {  // 255.0.0.0/8 (broadcast)
        update_stat(STAT_PACKETS_DROPPED);
        return XDP_DROP;
    }

    return XDP_PASS;
}

// License required for BPF programs
char _license[] SEC("license") = "GPL";

// Version information (optional)
__u32 _version SEC("version") = 1;

/*
 * Compilation instructions:
 *
 * clang -O2 -target bpf -c xdp_filter.c -o xdp_filter.o -I/usr/include/bpf
 *
 * Or with kernel headers:
 * clang -O2 -target bpf -c xdp_filter.c -o xdp_filter.o \
 *   -I/usr/src/linux-headers-$(uname -r)/include \
 *   -I/usr/src/linux-headers-$(uname -r)/arch/x86/include
 *
 * Loading:
 * xdp-loader load -m skb eth0 xdp_filter.o
 *
 * Checking status:
 * xdp-loader status eth0
 *
 * Viewing stats:
 * bpftool map dump name stats
 *
 * Setting rate limits (example):
 * bpftool map update name rate_limits key 0x0100007f value 100  # 127.0.0.1 -> 100 req/s
 *
 * Unloading:
 * xdp-loader unload eth0 --all
 */