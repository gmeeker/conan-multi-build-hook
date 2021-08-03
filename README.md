# Conan multi build hook

## Purpose

The Conan C++ package manager is designed to build one binary package per architecture, however, macOS and iOS can bundle multiple architectures into one file (called a universal or fat binary).  CMake supports multiple architectures, but many recipes use autotools or other methods.  Ideally Conan could handle all these cases:

1. CMake recipes build for multiple architectures (currently Conan requires a custom toolchain)
2. Recipes can handle this themselves, for example if they call xcodebuild with Xcode project files.
3. The other recipes in CCI should work without changes, by calling build() multiple times and joining with lipo (macOS/iOS).

This was discussed in this GitHub issue:
<https://github.com/conan-io/conan/issues/1047>
However, this iOS centric and ARM on macOS makes this urgent.

You could multiple -arch flags to your profile, but this doesn't work for recipes with processor specific code like OpenSSL.  You could run a script to run lipo after conan.  Or a solution could be integrated into Conan.  We can show this as a prototype using a hook.

## Hook setup

Place your hook Python files under *~/.conan/hooks*. The name of the hook would be the same one as the file name.

```
*~/.conan/hooks/conan-multi-build.py
```

Only copying hook files will not activate them.

### Conan config as installer

To install the hooks from Github:

``$ conan config install https://github.com/gmeeker/conan-multi-build-hook.git``

You can specify the source and destination folder to avoid copying undesired files to your local cache:

``$ conan config install https://github.com/gmeeker/conan-multi-build-hook.git -sf hooks -tf hooks ``

Conan config install does not activate any hook.

### Hook activation

You can activate the hook with:

``$ conan config set hooks.conan-multi-build``

### Settings

CMake handles universal binaries with CMAKE_OSX_ARCHITECTURES in the form "x86_64;arm64" (Xcode generator only).  We start by adding a similar setting to our profile:
```
[settings]
os=Macos
os_build=Macos
os.version=10.13
os.fat_arch=x86_64;armv8
arch=x86_64
arch_build=x86_64
compiler=apple-clang
compiler.version=12.0
compiler.libcxx=libc++
build_type=Release
compiler.cppstd=11
[options]
[build_requires]
darwin-toolchain/1.0.9@gmeeker/stable
```

And to settings.yml:
```
os:
    Macos:
        version: [None, "10.6", "10.7", "10.8", "10.9", "10.10", "10.11", "10.12", "10.13", "10.14", "10.15", "11.0", "13.0"]
        sdk: [None, "macosx"]
        subsystem: [None, catalyst]
        fat_arch: [None, ANY]
```

This choice reflects a Conan limitation that settings must be strings, not lists.  We can't just set ```arch=["x86_64","armv8"]```.  We also need to the Conan package id for existing recipes (which usually depend on 'os') so we can't just introduce a new setting.

### New conanfile.py methods

In order to let recipes handle this manually, we introduce two new methods: multi_build() and multi_package().  They behave exactly like build() and package() except that they should produce universal binaries fi os.fat_arch is defined.

### Hook details

To add support without requiring a custom version of Conan, this pre_build and pre_package hook will replace build() and package() for recipes.  This is disabled in some cases:

* Unsupported OS
* Header only recipe
* arch is not a dependency
* fat_arch is not defined or only arch is specified (note that this means you can't force a single architecture universal binary)
* recipe has options.multi_arch=False
* multi_build or multi_package is implemented (can be overriden independently)
* recipe's generator should handle this (currently only 'cmake')

The hook tries to duplicate a conanfile instance (because recipes may store generator instances or set cppflags).  This requires some assumptions about Conan's internals and it likely to break.  (This could be solved by implementing conanfile.copy() in the official source.)  A different approach would have been to load the conanfile again and apply a different profile or different settings.

build() just creates subdirectories for each architecture, changes settings.arch, build_folder, etc. and calls the original build().  This could be implemented more efficiently in the Conan source because we ignore no_copy_source (primarily to avoid more dependencies on the Conan internals).

package() runs multiple times for each architecture, like build(), into subdirectories in package_folder.  Then it assembles the final package by detecting binaries and running lipo, and copying normal files like header files.  It handles the cases where files are only produced in some architectures, like SSE headers or libraries not being present.

package_info() is not currently overriden, if any recipes look at settings.arch here.

### CMake

By default, Conan does not use the CMake Xcode generator and won't build multiple architectures.  Recipes should work, but it's also possible to build all architectures simultaneously.

Conan will always set CMAKE_OSX_ARCHITECTURES to a single arch, unless a toolchain is specified, and Conan's CMake support relies on os.environ which is easier to set from a package.

One approach is here: <https://github.com/gmeeker/conan-darwin-toolchain> which is forked from <https://github.com/theodelrieu/conan-darwin-toolchain> and updated for multiple architectures.  Add the package to [build_requires].

Set CONAN_CMAKE_GENERATOR=Xcode which the hook will detect and the toolchain will handle the rest.  The toolchain will set CONAN_CMAKE_OSX_ARCHITECTURES from os.fat_arch.

Note that only the CMake generator uses CONAN_CMAKE_TOOLCHAIN_FILE and the CMake tool does not.  Ideally CMake support for multiple architectures should be addressed in the Conan source itself.

### Code signing

Code signing is an example of an operation that should be performed on the final binary.  This should be handled by using the cmake generator or implementing multi_build() and mult_package().  There doesn't appear to be a standard Conan setting for this.

## To Do

iOS support should probably combine various SDKs and architectures to support iOS and simulators.  One such toolchain is here:
<https://github.com/leetal/ios-cmake> although it doesn't yet support Mac universal binaries or control of individual architectures.

Android support sounds similar but is not addressed here.

CMake support shouldn't require a toolchain, but this would require patching Conan itself.
