import copy
import os
import shutil
import subprocess
import textwrap
import types

from conans.errors import ConanException
from conans import tools
from conans.client.file_copier import FileCopier
from conans.model.build_info import DepsCppInfo
from conans.model.conan_file import get_env_context_manager
from conans.client.tools.apple import is_apple_os
from conans.client.build.cmake_flags import get_generator

required_conan_version = ">=1.37.0"

# Can be overriden in settings / profile
_multi_arch_generators = ['cmake']

# Arch subfolder
_arch_folder = 'conan_archs'

# These are for optimization only, to avoid unnecessarily reading files.
_binary_exts = ['.a', '.dylib']
_regular_exts = [
    '.h', '.hpp', '.hxx', '.c', '.cc', '.cxx', '.cpp', '.m', '.mm', '.txt', '.md', '.html', '.jpg', '.png'
]

def multi_arch_generators(conanfile):
    generators = _multi_arch_generators
    try:
        return conanfile.settings.multi_arch_generators
    except ConanException:
        pass
    conanfile.output.info("Generator %s" % get_generator(conanfile))
    with get_env_context_manager(conanfile):
        if get_generator(conanfile) == "Xcode":
            return generators
    return [gen for gen in generators if gen != "cmake"]

def supported_os(os):
    # ['Macos', 'iOS', 'watchOS', 'tvOS']
    return is_apple_os(os) # or Android?

def get_archs(conanfile):
    try:
        return str(conanfile.settings.os.fat_arch).split(';')
    except AttributeError:
        return
    return [conanfile.settings.arch]

def is_macho_binary(filename):
    ext = os.path.splitext(filename)[1]
    if ext in _binary_exts:
        return True
    if ext in _regular_exts:
        return False
    with open(filename, "rb") as f:
        header = f.read(4)
        if header == b'\xcf\xfa\xed\xfe':
            # cffaedfe is Mach-O binary
            return True
        elif header == b'\xca\xfe\xba\xbe':
            # cafebabe is Mach-O fat binary
            return True
        elif header == b'!<arch>\n':
            # ar archive
            return True
    return False

def copy_arch_file(conanfile, src, dst, top=None, archs=[]):
    if os.path.isfile(src):
        if top and archs and is_macho_binary(src):
            # Try to lipo all available archs on the first path.
            src_components = src.split(os.path.sep)
            top_components = top.split(os.path.sep)
            if src_components[:len(top_components)] == top_components:
                arch_dir = src_components[len(top_components)]
                subpath = src_components[len(top_components) + 2:]
                arch_paths = [os.path.join(*([top, arch_dir, arch] + subpath)) for arch in archs]
                arch_paths = [p for p in arch_paths if os.path.isfile(p)]
                if len(arch_paths) > 1:
                    conanfile.run(['lipo', '-create', '-output', dst] + arch_paths)
                    return
        if os.path.exists(dst):
            pass # don't overwrite existing files
        else:
            shutil.copy2(src, dst)

# Modified copytree to copy new files to an existing tree.
def graft_tree(src, dst, symlinks=False, copy_function=shutil.copy2, dirs_exist_ok=False):
    names = os.listdir(src)
    os.makedirs(dst, exist_ok=dirs_exist_ok)
    errors = []
    for name in names:
        srcname = os.path.join(src, name)
        dstname = os.path.join(dst, name)
        if os.path.exists(dstname):
            continue
        try:
            if symlinks and os.path.islink(srcname):
                linkto = os.readlink(srcname)
                os.symlink(linkto, dstname)
            elif os.path.isdir(srcname):
                graft_tree(srcname, dstname, symlinks, copy_function, dirs_exist_ok)
            else:
                copy_function(srcname, dstname)
            # XXX What about devices, sockets etc.?
        except OSError as why:
            errors.append((srcname, dstname, str(why)))
        # catch the Error from the recursive graft_tree so that we can
        # continue with other files
        except Error as err:
            errors.extend(err.args[0])
    try:
        shutil.copystat(src, dst)
    except OSError as why:
        # can't copy file access times on Windows
        if why.winerror is None:
            errors.extend((src, dst, str(why)))
    if errors:
        raise shutil.Error(errors)

def conanfile_copy(conanfile):
    result = copy.copy(conanfile)
    result._build1 = types.MethodType(result.__class__.build, result)
    result._package1 = types.MethodType(result.__class__.package, result)
    result._test1 = types.MethodType(result.__class__.test, result)
    result.settings = conanfile.settings.copy()
    # result.options = conanfile.options.copy()
    # result.layout = copy.deepcopy(conanfile.layout)
    result.folders = copy.deepcopy(conanfile.folders)
    # result.deps_cpp_info = copy.copy(result.deps_cpp_info)
    # result.deps_cpp_info = DepsCppInfo()
    # result.deps_cpp_info.update(conanfile.deps_cpp_info)
    return result

def cmake_system_name(conanfile):
    if conanfile.settings.os == "Macos":
        return "Darwin"
    return str(conanfile.settings.os)

def cmake_system_processor(conanfile):
    return {"x86": "i386",
            "x86_64": "x86_64",
            "armv7": "arm",
            "armv8": "aarch64"}.get(str(conanfile.settings.arch))

_toolchain = textwrap.dedent('''
    if ((CMAKE_MAJOR_VERSION GREATER_EQUAL 3) AND (CMAKE_MINOR_VERSION GREATER_EQUAL 14))
      # CMake 3.14 added support for Apple platform cross-building
      # Platform/CMAKE_SYSTEM_NAME.cmake will be called later
      # Those files have broken quite a lot of things
      set(CMAKE_SYSTEM_NAME $ENV{CONAN_CMAKE_SYSTEM_NAME})
    else()
      set(CMAKE_SYSTEM_NAME Darwin)
    endif()
    set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM BOTH)
    set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE BOTH)
    set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY BOTH)
    set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE NEVER)
    set(CMAKE_OSX_DEPLOYMENT_TARGET $ENV{CONAN_CMAKE_OSX_DEPLOYMENT_TARGET})
    set(CMAKE_OSX_ARCHITECTURES $ENV{CONAN_CMAKE_OSX_ARCHITECTURES})
    set(CMAKE_OSX_SYSROOT $ENV{CONAN_CMAKE_OSX_SYSROOT})
    # Setting CMAKE_SYSTEM_NAME results it CMAKE_SYSTEM_VERSION not being set
    # For some reason, it must be the Darwin version (otherwise Platform/Darwin.cmake will not set some flags)
    # Most probably a CMake bug... (https://gitlab.kitware.com/cmake/cmake/issues/20036)
    set(CMAKE_SYSTEM_VERSION "${CMAKE_HOST_SYSTEM_VERSION}")
    set(CMAKE_SYSTEM_PROCESSOR "$ENV{CONAN_CMAKE_SYSTEM_PROCESSOR}")
''')

def setup_cmake(conanfile):
    if not supported_os(conanfile.settings.os):
        return
    def to_apple_arch(arch):
        if conanfile.settings.os == "watchOS" and arch == "armv8":
            return "arm64_32"
        return tools.to_apple_arch(arch)
    darwin_arch = [to_apple_arch(arch) for arch in get_archs(conanfile)]

    xcrun = tools.XCRun(conanfile.settings)
    sysroot = xcrun.sdk_path

    os.environ["CONAN_CMAKE_SYSTEM_NAME"] = cmake_system_name(conanfile)
    if conanfile.settings.get_safe("os.version"):
        os.environ["CONAN_CMAKE_OSX_DEPLOYMENT_TARGET"] = str(conanfile.settings.os.version)
    os.environ["CONAN_CMAKE_OSX_ARCHITECTURES"] = ";".join(darwin_arch)
    os.environ["CONAN_CMAKE_OSX_SYSROOT"] = sysroot
    os.environ["CONAN_CMAKE_SYSTEM_PROCESSOR"] = cmake_system_processor(conanfile)
    if not os.environ.get("CONAN_CMAKE_TOOLCHAIN_FILE", None):
        toolchain = os.path.join(os.path.dirname(__file__), "darwin-toolchain.cmake")
        if not os.path.exists(toolchain):
            with open(toolchain, "w") as f:
                f.write(_toolchain)
        os.environ["CONAN_CMAKE_TOOLCHAIN_FILE"] = toolchain

def multi_build(self_):
    archs = get_archs(self_)
    if len(archs) > 1:
        settings = self_.settings
        build_folder = self_.build_folder
        package_folder = self_.package_folder
        def ignore_archs(path, files):
            if path == build_folder:
                if _arch_folder in files:
                    return [_arch_folder]
            return [] # ignore nothing
        for arch in archs:
            conanfile = conanfile_copy(self_)
            conanfile.display_name = '%s[%s]' % (self_.display_name, arch)
            conanfile.settings.arch = arch
            conanfile.settings.os.fat_arch = None
            conanfile.build_folder = os.path.join(build_folder, _arch_folder, arch)
            conanfile.install_folder = conanfile.build_folder
            conanfile.package_folder = os.path.join(package_folder, _arch_folder, arch)
            shutil.copytree(build_folder,
                            conanfile.build_folder,
                            symlinks=True,
                            ignore=ignore_archs)
            with tools.chdir(conanfile.build_folder):
                conanfile.output.info("building arch: %s" % (arch,))
                conanfile.output.info(conanfile.settings.items())
                conanfile._build1()
    else:
        self_._build1()

def multi_test(self_):
    archs = get_archs(self_)
    if len(archs) > 1:
        settings = self_.settings
        build_folder = self_.build_folder
        package_folder = self_.package_folder
        for arch in archs:
            conanfile = conanfile_copy(self_)
            conanfile.deps_cpp_info = self_.deps_cpp_info
            conanfile.display_name = '%s[%s]' % (self_.display_name, arch)
            conanfile.settings.arch = arch
            conanfile.settings.os.fat_arch = None
            conanfile.build_folder = os.path.join(build_folder, _arch_folder, arch)
            conanfile.install_folder = conanfile.build_folder
            conanfile.package_folder = os.path.join(package_folder, _arch_folder, arch)
            with tools.chdir(conanfile.build_folder):
                conanfile._test1()
    else:
        self_._test1()

def safe_package(self_):
    # Some packages (libpng) use cmake but package with self.copy("*")
    # so make sure that we don't find the single arch files.
    copy = self_.copy
    def copy_(*args, excludes=[], **kw):
        copy(*args, excludes=excludes + ["*Objects-normal"], **kw)
    self_.copy = copy_
    self_._package1()

def multi_package(self_):
    archs = get_archs(self_)
    if len(archs) > 1:
        settings = self_.settings
        build_folder = self_.build_folder
        package_folder = self_.package_folder
        for arch in archs:
            conanfile = conanfile_copy(self_)
            conanfile.display_name = '%s[%s]' % (self_.display_name, arch)
            conanfile.settings.arch = arch
            conanfile.settings.os.fat_arch = None
            conanfile.build_folder = os.path.join(build_folder, _arch_folder, arch)
            conanfile.install_folder = conanfile.build_folder
            conanfile.package_folder = os.path.join(package_folder, _arch_folder, arch)
            with tools.chdir(conanfile.build_folder):
                folders = [conanfile.source_folder, conanfile.build_folder]
                conanfile.copy = FileCopier(folders, conanfile.package_folder)
                conanfile.output.info("packaging arch: %s" % (arch,))
                conanfile.output.info(conanfile.settings)
                conanfile._package1()
        for arch in archs:
            graft_tree(os.path.join(package_folder, _arch_folder, arch),
                       package_folder,
                       symlinks=True,
                       copy_function=lambda s, d: copy_arch_file(conanfile, s, d, top=package_folder, archs=archs),
                       dirs_exist_ok=True)
        shutil.rmtree(os.path.join(self_.package_folder, _arch_folder))
    else:
        self_._package1()

def supports_multi_arch(conanfile):
    try:
        return conanfile.settings.multi_arch
    except ConanException:
        pass
    try:
        return conanfile.multi_arch
    except AttributeError:
        pass
    for generator in conanfile.generators:
        if generator in multi_arch_generators(conanfile):
            return True
    try:
        conanfile.multi_build
        conanfile.multi_package
    except AttributeError:
        return False
    return True

def patch_conanfile(conanfile):
    try:
        if conanfile.options.header_only:
            # Header only
            return
    except ConanException:
        pass
    if not supported_os(conanfile.settings.get_safe("os")):
        # Unsupported OS
        return
    try:
        conanfile.settings.arch
    except ConanException:
        # Arch is not required, so don't try to compile more than once
        return
    if len(get_archs(conanfile)) <= 1:
        return
    # if "cmake" in conanfile.generators and "cmake" in multi_arch_generators(conanfile):
    #     setup_cmake(conanfile)
    if getattr(conanfile, "_package1", None):
        # already patched
        return
    if supports_multi_arch(conanfile):
        conanfile._package1 = conanfile.package
        conanfile.multi_package = types.MethodType(safe_package, conanfile)
        conanfile.package = conanfile.multi_package
        return
    conanfile.output.info("Enable multi build for %s" % (conanfile.display_name))
    conanfile._build1 = conanfile.build
    conanfile._package1 = conanfile.package
    conanfile._test1 = conanfile.test
    try:
        conanfile.build = conanfile.multi_build
    except AttributeError:
        conanfile.multi_build = types.MethodType(multi_build, conanfile)
        conanfile.build = conanfile.multi_build
    try:
        conanfile.package = conanfile.multi_package
    except AttributeError:
        conanfile.multi_package = types.MethodType(multi_package, conanfile)
        conanfile.package = conanfile.multi_package
    try:
        conanfile.test = conanfile.multi_test
    except AttributeError:
        conanfile.multi_test = types.MethodType(multi_test, conanfile)
        conanfile.test = conanfile.multi_test

def pre_build(output, conanfile, **kwargs):
    patch_conanfile(conanfile)

def pre_package(output, conanfile, conanfile_path, **kwargs):
    patch_conanfile(conanfile)
