from setuptools import setup, find_packages

setup(
    name="bifrost-relayer-lib",
    version="v0.2.0",
    package=find_packages,
    install_requires=[],
    dependency_links=["git+https://github.com/bifrost-platform/bifrost-python-lib.git"]
)
