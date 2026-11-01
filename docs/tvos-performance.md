# Apple TV performance investigation

The A12 build boots, renders, accepts controller/gyro input, and supports remote inspection. No trace yet establishes its dominant gameplay bottleneck or consistent 4K60 delivery. This document preserves the initial source audit and the next experiments; it does not label audit candidates as measured defects.

## Establish the baseline

Start with the current native-mod-disabled build and a backed-up save. Record app and dependency commits, catalog/context, profiling state, output mode, internal resolution, shadows, interpolation, texture replacements and controller configuration. Separate cold shader/pipeline compilation from warmed runs. Repeat fixed overworld and dungeon routes, room transitions, dense effects and camera sweeps. Include sustained runs to detect thermal or memory-pressure changes.

Measure CPU active work and waits, simulation ticks per presentation, GPU pass times when valid, raw frame/present intervals, missed budgets, memory and compilation stalls. Report distributions (p50/p95/p99), not only an average. A 60 Hz presentation budget is about 16.67 ms; 30 Hz simulation does not imply 60 Hz presentation is impossible, but interpolation and its cost must be identified.

Existing `pollFps` is loop cadence sampled at 1–10 Hz. It cannot establish GPU time, scanout cadence or tail latency. Screenshots prove a rendered state, not motion quality or 4K60 throughput. Current screenshots are bounded, intrusive diagnostic captures; exclude their readback/PNG work from normal benchmark windows.

## Reuse the existing profiler

Aurora already has CPU recording, frame-slot/pacing wait plots, render-worker statistics, GPU pass timestamp support and Tracy zones. The validated normal build has Tracy disabled. Try a separate instrumented build before creating another profiler, and compare instrumented/uninstrumented overhead.

Inspect [Aurora frame recording](../extern/aurora/lib/gfx/frame.cpp), [GPU timestamps](../extern/aurora/lib/webgpu/gpu_prof.cpp), [public stats](../extern/aurora/include/aurora/gfx.h), and the [application loop](../src/m_Do/m_Do_main.cpp). CPU recording time excludes other main-loop work. Present-period plots are smoothed CPU-observed intervals, not confirmed scanout. GPU timestamps depend on runtime feature support, have asynchronous frame association and Metal-specific handling, and can skip samples. A12 validity and availability must be verified.

Add a bounded raw sample buffer with frame IDs, tick counts, work/wait spans and GPU valid/skipped counts. Export batches through the debug connection after capture, rather than synchronously logging JSON for every frame. Associate render-worker counters with the frame that produced them. Existing debug commands can stage positions/camera/time; deterministic replay and a dedicated torture/benchmark runner have not been implemented.

## Source audit candidates

| Candidate | Evidence and discriminating measurement |
| --- | --- |
| Serial simulation/draw path | [Main loop](../src/m_Do/m_Do_main.cpp) and [process manager](../src/f_pc/f_pc_manager.cpp) sequence game phases. Time each phase and catch-up ticks before moving independent work to workers. |
| Matrix interpolation bookkeeping | [Frame interpolation](../src/dusk/interp/frame_interpolation.cpp) rebuilds capture/replacement hash state. Count records, allocations, rehashes and phase time; compare buffer reuse or stable indices with real key counts. |
| Deformed vertex traffic | [Vertex interpolation](../src/dusk/interp/vertex.cpp) retains/copies/interpolates eligible position and normal arrays. Measure bytes, capacities and time at matched scenes and presentation rates. |
| Serialized loading | [DVD worker](../src/m_Do/m_Do_dvd_thread.cpp) drains callbacks serially; [decompressor](../libs/JSystem/src/JKernel/JKRDvdRipper.cpp) holds shared state/lock through reads and decompression. Measure queue delay, refill sizes and lock hold/wait before adding workers. |
| Archive extraction copy | [Archive extraction](../src/dusk/archive.cpp) copies an extracted allocation into a vector under its archive lock. Measure frequency, sizes and peak live memory; investigate direct destination extraction if material. |
| Mutex side tables | [OSMutex compatibility](../src/dusk/OSMutex.cpp) serializes native-handle lookups through a global map lock. Measure lookup contention and preserve ABI/lifetime semantics when changing it. |
| Visibility traversal | [Actor dispatch](../src/f_op/f_op_actor.cpp) and [culling](../src/f_op/f_op_actor_mng.cpp) perform actor-specific clipping. Count tested/culled/drawn actors and special paths before changing distance or introducing another spatial structure. |

These observations came from an initial application-layer audit. Dependencies were initially absent; subsequent inspection found existing renderer workers, caches and profiling support. Recheck the complete renderer before claiming a missing facility or duplicate optimization. Interrupt/scheduler compatibility stubs do not automatically provide native-thread synchronization; review correctness before increasing concurrency.

## Draw distance and presentation

Actor culling already supports boxes/spheres, actor-specific far multipliers and event adjustments. There is no validated global distance control covering terrain, particles, shadows and special draws. Visibility flags also affect execution policy; drawing more or culling differently can change gameplay if that coupling is ignored.

Define fixed camera sweeps and traversal paths through overworld/dungeon scenes. Record when actors or geometry appear/disappear, correlate those events with visibility counters and frame cost, and compare identical scene state. Test increased or adaptive distance as a reversible feature after the baseline. Preserve event exceptions, conservative bounds, room behavior and continuity across transitions.

## Choose the optimization from evidence

Rust integration is already present and is an ordinary implementation option. Ownership and columnar storage can suit stable handles, contiguous records and parallel kernels; they do not by themselves prove a speedup. Compare against the existing implementation at equivalent correctness and workload.

Evaluate CPU jobs, GPU interpolation/culling, upscaling and denoising where measurements identify useful work. Moving work to the GPU can worsen a GPU-bound frame; multithreading arbitrary actors can alter shared globals, lifecycle, RNG, collision, audio and event order. Preserve those semantics and measure synchronization/data-transfer costs. Access to A12 CPU/GPU or other accelerators must come through an available, validated platform API; no untapped-accelerator benefit is assumed.

The next decision is the first measured bottleneck worth changing. Retain scene/settings, traces, visual evidence, correctness checks and before/after distributions with each experiment. Version/promote a reliable candidate through the explicit [release process](releases.md), independently of optimization or merges into `main`.
