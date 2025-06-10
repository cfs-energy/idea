import ideavirtualdesktopcontroller_meta
from setuptools import find_packages, setup

setup(
    name=ideavirtualdesktopcontroller_meta.__name__,
    version=ideavirtualdesktopcontroller_meta.__version__,
    description='idea virtual-desktop controller',
    url='https://awslabs.github.io/scale-out-computing-on-aws/',
    author='Amazon',
    license='Apache License, Version 2.0',
    packages=find_packages(),
    package_dir={'ideavirtualdesktopcontroller': 'ideavirtualdesktopcontroller'},
    entry_points="""
        [console_scripts]
        ideactl=ideavirtualdesktopcontroller.cli.cli_main:main
        ideaserver=ideavirtualdesktopcontroller.app.app_main:main
    """,
)
