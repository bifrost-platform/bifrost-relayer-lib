from setuptools import setup, find_packages

setup(
    name="bifrost-relayer-lib",
    version="0.3.0",
    package=find_packages,
    install_requires=[
        "chainpy @ git+https://github.com/bifrost-platform/bifrost-python-lib.git"
    ]
)
