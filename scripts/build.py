#!/usr/bin/env python3
"""Build the Apple Silicon app locally; never connects to a camera."""
import importlib.metadata
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / '.build'
DIST = ROOT / 'dist'


def run(*args):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True)


def main():
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('This build currently requires an Apple Silicon Mac and arm64 Python.')
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    app = BUILD / 'bundle' / '影石助手.app'
    if app.exists():
        shutil.rmtree(app)
    resources = app / 'Contents' / 'Resources'
    executable_dir = app / 'Contents' / 'MacOS'
    resources.mkdir(parents=True)
    executable_dir.mkdir()
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
        '--name', 'CameraBridge', '--collect-all', 'av',
        '--distpath', BUILD / 'backend-dist', '--workpath', BUILD / 'pyinstaller',
        '--specpath', BUILD, ROOT / 'backend' / 'app_backend.py')
    shutil.copytree(BUILD / 'backend-dist' / 'CameraBridge', resources / 'Backend', symlinks=True)
    run('xcrun', 'swiftc', '-target', 'arm64-apple-macosx14.0', '-swift-version', '5',
        '-parse-as-library', '-O', '-framework', 'SwiftUI', '-framework', 'AppKit',
        ROOT / 'app' / 'App.swift', '-o', executable_dir / 'YingShiAssistant')
    with (ROOT / 'app' / 'Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    info['CFBundleShortVersionString'] = (ROOT / 'VERSION').read_text().strip()
    with (app / 'Contents' / 'Info.plist').open('wb') as stream:
        plistlib.dump(info, stream)
    iconset = BUILD / 'Assistant.iconset'
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    run('xcrun', 'swiftc', ROOT / 'scripts' / 'icon.swift', '-o', BUILD / 'make-icon')
    run(BUILD / 'make-icon', iconset)
    run('iconutil', '-c', 'icns', iconset, '-o', resources / 'Assistant.icns')

    source = resources / 'Source'
    source.mkdir()
    for folder in ('app', 'backend', 'scripts', 'tests', 'docs'):
        shutil.copytree(ROOT / folder, source / folder,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
    for filename in ('LICENSE', 'NOTICE', 'README.md', 'VERSION', 'requirements.txt'):
        shutil.copy2(ROOT / filename, source / filename)
    licenses = resources / 'ThirdPartyLicenses'
    licenses.mkdir()
    for name in ('av', 'Pillow', 'PyInstaller'):
        distribution = importlib.metadata.distribution(name)
        for file in distribution.files or []:
            if '.dist-info/' in str(file) and any(
                word in str(file).lower() for word in ('license', 'copying', 'notice')
            ):
                target = licenses / name / str(file)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(distribution.locate_file(file), target)
    python_license = Path(sys.base_prefix) / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'LICENSE.txt'
    if python_license.exists():
        shutil.copy2(python_license, licenses / 'Python-LICENSE.txt')

    run('codesign', '--force', '--deep', '--sign', '-', app)
    run('codesign', '--verify', '--deep', '--strict', app)
    run(resources / 'Backend' / 'CameraBridge', '--self-test')
    destination = DIST / app.name
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(app, destination, symlinks=True)
    archive = DIST / '影石助手.zip'
    archive.unlink(missing_ok=True)
    run('/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', destination, archive)
    print(f'Built: {destination}\nArchive: {archive}')


if __name__ == '__main__':
    main()
