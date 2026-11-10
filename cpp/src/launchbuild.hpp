// Steam / Lutris / Heroic launch-options string builder (the Game Tools "launch-options builder" box).
#pragma once
#include <string>

namespace tt::launchbuild {

struct Opts {
    bool mangohud = false, mh_off = false, mh_dlsym = false, ac_keep_mh = false;
    bool gamemode = true, gamescope = false;
    bool prime = false, nvcache = false, nv_threaded = false;
    bool radv_gpl = false, radv_full = false, mesa_cache = false, no_vsync = false;
    bool dxvk_cache = true, dxvk_async = false, proton_nolog = true, ntsync = true;
    bool anticheat = false;
    bool staging_shm = true, gl_yield = true, gl_maxframes = true, gl_vrr = true, glthread = true;
    std::string w = "1920", h = "1080", fps;   // gamescope -W / -H / -r
};

// Defaults depend on the GPUs present (PRIME + NVIDIA cache on NVIDIA machines; RADV/Mesa cache on AMD-only).
Opts defaults(bool has_nvidia, bool has_amd);

// cache_dir is the raw configured folder (a leading "~" becomes $HOME so the string stays portable).
std::string build(const Opts& o, const std::string& cache_dir, int cache_gb, const std::string& user);

}  // namespace tt::launchbuild
