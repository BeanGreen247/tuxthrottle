#include "launchbuild.hpp"

#include <algorithm>
#include <vector>

#include "util.hpp"

namespace tt::launchbuild {

Opts defaults(bool has_nvidia, bool has_amd) {
    Opts o;
    o.prime = has_nvidia;
    o.nvcache = has_nvidia;
    o.radv_gpl = has_amd && !has_nvidia;
    o.mesa_cache = has_amd && !has_nvidia;
    o.radv_full = has_amd;
    return o;
}

static bool digits(const std::string& s) {
    std::string t = trim(s);
    return !t.empty() && std::all_of(t.begin(), t.end(), [](unsigned char c) { return std::isdigit(c) != 0; });
}

std::string build(const Opts& o, const std::string& cache_dir, int gb, const std::string& user) {
    std::vector<std::string> env, wrap;
    std::string base = cache_dir;
    if (!base.empty() && base[0] == '~') base = "$HOME" + base.substr(1);
    if (o.prime) {
        env.push_back("__NV_PRIME_RENDER_OFFLOAD=1");
        env.push_back("__VK_LAYER_NV_optimus=NVIDIA_only");
        env.push_back("__GLX_VENDOR_LIBRARY_NAME=nvidia");
    }
    if (o.nvcache) {
        env.push_back("__GL_SHADER_DISK_CACHE=1");
        env.push_back("__GL_SHADER_DISK_CACHE_PATH=" + base + "/nv-shader-cache");
        env.push_back("__GL_SHADER_DISK_CACHE_SIZE=" + std::to_string(static_cast<long long>(gb) * 1000000000LL));
        env.push_back("__GL_SHADER_DISK_CACHE_SKIP_CLEANUP=1");
    }
    if (o.nv_threaded) env.push_back("__GL_THREADED_OPTIMIZATIONS=1");
    if (o.radv_full) env.push_back("RADV_PERFTEST=gpl,nggc,sam");
    else if (o.radv_gpl) env.push_back("RADV_PERFTEST=gpl");
    if (o.staging_shm) env.push_back("STAGING_SHARED_MEMORY=1");
    if (o.gl_yield) env.push_back("__GL_YIELD=USLEEP");
    if (o.gl_maxframes) env.push_back("__GL_MaxFramesAllowed=2");
    if (o.gl_vrr) env.push_back("__GL_VRR_ALLOWED=1");
    if (o.glthread && !o.no_vsync) env.push_back("mesa_glthread=true");
    if (o.mesa_cache) {
        env.push_back("MESA_GLSL_CACHE_ENABLE=1");
        env.push_back("MESA_SHADER_CACHE_DIR=" + base + "/mesa-shader-cache");
        env.push_back("MESA_SHADER_CACHE_MAX_SIZE=" + std::to_string(gb) + "G");
    }
    if (o.no_vsync) { env.push_back("vblank_mode=0"); env.push_back("mesa_glthread=true"); }
    if (o.dxvk_cache) {
        env.push_back("DXVK_STATE_CACHE=1");
        env.push_back("DXVK_STATE_CACHE_PATH=" + base + "/dxvk-state-cache");
    }
    if (o.dxvk_async) env.push_back("DXVK_ASYNC=1");
    if (o.proton_nolog) env.push_back("PROTON_LOG=0");
    if (o.ntsync) env.push_back("PROTON_USE_NTSYNC=1");
    bool keep_mh = o.anticheat && o.ac_keep_mh;
    if (o.anticheat) {
        env.push_back("PROTON_BATTLEYE_RUNTIME=\"/home/" + user + "/.local/share/Steam/steamapps/common/Proton BattlEye Runtime\"");
        env.push_back("DISABLE_VKBASALT=1");
        env.push_back("VK_LOADER_LAYERS_DISABLE=~implicit~");
        if (!keep_mh) env.push_back("MANGOHUD=0");
    }
    bool mh_off = o.mh_off && !keep_mh;
    if (mh_off && std::find(env.begin(), env.end(), "MANGOHUD=0") == env.end()) env.insert(env.begin(), "MANGOHUD=0");
    if (o.gamemode) wrap.push_back("gamemoderun");
    if (o.mangohud && !mh_off && (!o.anticheat || keep_mh)) {
        if (o.mh_dlsym) env.push_back("MANGOHUD_DLSYM=1");
        wrap.push_back("mangohud");
    }
    std::vector<std::string> out;
    if (o.gamescope) {
        out.push_back("gamescope");
        if (digits(o.w)) { out.push_back("-W"); out.push_back(trim(o.w)); }
        if (digits(o.h)) { out.push_back("-H"); out.push_back(trim(o.h)); }
        if (digits(o.fps)) { out.push_back("-r"); out.push_back(trim(o.fps)); }
        out.push_back("-f");
        out.push_back("--");
        if (!env.empty()) { out.push_back("env"); out.insert(out.end(), env.begin(), env.end()); }
        out.insert(out.end(), wrap.begin(), wrap.end());
    } else {
        out = env;
        out.insert(out.end(), wrap.begin(), wrap.end());
    }
    out.push_back("%command%");
    return join(out, " ");
}

}  // namespace tt::launchbuild
