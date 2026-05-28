# ruff: noqa
@enforce_types
def chmod_file(path: str, cwd: str = "", config=None, **config_kwargs) -> None:
    """chmod -R <permissions> <cwd>/<path>"""

    root = Path(cwd or os.getcwd()) / path
    if not os.access(root, os.R_OK):
        raise Exception(f"Failed to chmod: {path} does not exist (did the previous step fail?)")

    if not root.is_dir():
        # path is just a plain file
        config = config or get_config(**config_kwargs)
        os.chmod(root, int(config.OUTPUT_PERMISSIONS, base=8))
    else:
        config = config or get_config(**config_kwargs)
        for subpath in Path(path).glob("**/*"):
            if subpath.is_dir():
                # directories need execute permissions to be able to list contents
                os.chmod(subpath, int(config.DIR_OUTPUT_PERMISSIONS, base=8))
            else:
                os.chmod(subpath, int(config.OUTPUT_PERMISSIONS, base=8))


@enforce_types
def copy_and_overwrite(from_path: str | Path, to_path: str | Path):
    """copy a given file or directory to a given path, overwriting the destination"""

    assert os.access(from_path, os.R_OK)

    if Path(from_path).is_dir():
        shutil.rmtree(to_path, ignore_errors=True)
        shutil.copytree(from_path, to_path)
    else:
        with open(from_path, "rb") as src:
            contents = src.read()
        atomic_write(to_path, contents)


class suppress_output:
    """
    A context manager for doing a "deep suppression" of stdout and stderr in
    Python, i.e. will suppress all print, even if the print originates in a
    compiled C/Fortran sub-function.

    This will not suppress raised exceptions, since exceptions are printed
    to stderr just before a script exits, and after the context manager has
    exited (at least, I think that is why it lets exceptions through).

    with suppress_stdout_stderr():
        rogue_function()
    """

    def __init__(self, stdout=True, stderr=True):
        # Open a pair of null files
        # Save the actual stdout (1) and stderr (2) file descriptors.
        self.stdout, self.stderr = stdout, stderr
        if stdout:
            self.null_stdout = os.open(os.devnull, os.O_RDWR)
            self.real_stdout = os.dup(1)
        if stderr:
            self.null_stderr = os.open(os.devnull, os.O_RDWR)
            self.real_stderr = os.dup(2)

    def __enter__(self):
        # Assign the null pointers to stdout and stderr.
        if self.stdout:
            os.dup2(self.null_stdout, 1)
        if self.stderr:
            os.dup2(self.null_stderr, 2)

    def __exit__(self, *_):
        # Re-assign the real stdout/stderr back to (1) and (2)
        if self.stdout:
            os.dup2(self.real_stdout, 1)
            os.close(self.null_stdout)
        if self.stderr:
            os.dup2(self.real_stderr, 2)
            os.close(self.null_stderr)
