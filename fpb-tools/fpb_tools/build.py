import logging

import docker
import docker.errors
from docker.utils import tar
from pathlib import Path

from fpb_tools.utils import run_shell, get_logger, zip_step_returns, HandlerRet
from fpb_tools.device import FW_FLASH_SIZE, FW_EEPROM_SIZE
from fpb_tools.subparsers import (
    SubparserBuildEnv,
    SubparserBuildCarFobPair,
)


async def env(
    design: Path,
    name: str,
    image: str = SubparserBuildEnv.image,
    docker_dir: Path = SubparserBuildEnv.docker_dir,
    dockerfile: str = SubparserBuildEnv.dockerfile,
    logger: logging.Logger = None,
) -> HandlerRet:
    tag = f"{image}:{name}"
    logger = logger or get_logger()
    logger.info(f"Building image {tag}")

    # Add build directory to context
    build_dir = design.resolve() / docker_dir
    dockerfile_name = build_dir / dockerfile
    with open(dockerfile_name, "r") as df:
        dockerfile = ("Dockerfile", df.read())
    dockerfile = tar(build_dir, dockerfile=dockerfile)

    # run docker build
    client = docker.from_env()
    try:
        _, logs_raw = client.images.build(
            tag=tag, fileobj=dockerfile, custom_context=True,
        )
    except docker.errors.BuildError as e:
        logger.error(f"Docker build error: {e}")
        for log in e.build_log:
            if "stream" in log and log["stream"].strip():
                logger.error(log["stream"].strip())
        raise
    logger.info(f"Built image {tag}")

    logs = "".join([d["stream"] for d in list(logs_raw) if "stream" in d])
    logging.debug(logs)
    return logs.encode(), b""


async def car_fob_pair(
    design: Path,
    name: str,
    deployment: str,
    car_name: str,
    car_out: Path,
    car_id: int,
    car_in: Path = SubparserBuildCarFobPair.car_in,
    image: str = SubparserBuildCarFobPair.image,
    logger: logging.Logger = None,
) -> HandlerRet:
    """
    Build car and paired fob pair
    """

    # Image information
    tag = f"{image}:{name}"
    logger = logger or get_logger()
    logger.info(f"{tag}:{deployment}: Building car {car_name}")

    # Car defines
    car_defines = f" CAR_ID={car_id}"

    # Build car
    car_output = await make_dev(
        image=image,
        name=name,
        design=design,
        deployment=deployment,
        dev_name=car_name,
        dev_in=car_in,
        dev_out=car_out,
        defines=car_defines,
        make_target="car",
        logger=logger,
    )

    return zip_step_returns([car_output])




async def make_dev(
    image: str,
    name: str,
    design: str,
    deployment: str,
    dev_name: str,
    dev_in: Path,
    dev_out: Path,
    defines: str,
    make_target: str,
    logger: logging.Logger,
) -> HandlerRet:
    """
    Build device firmware
    """
    tag = f"{image}:{name}"

    # Setup full container paths
    bin_path = f"/dev_out/{dev_name}.bin"
    elf_path = f"/dev_out/{dev_name}.elf"
    eeprom_path = f"/dev_out/{dev_name}.eeprom"
    dev_in = (design / dev_in).resolve()
    dev_out = dev_out.resolve()

    # Create output directory
    if not dev_out.exists():
        logger.info(f"{tag}:{deployment}: Making output directory {dev_out}")
        dev_out.mkdir()

    # Compile
    output = await run_shell(
        "docker run"
        f' -v "{str(dev_in)}":/dev_in:ro'
        f' -v "{str(dev_out)}":/dev_out'
        f" -v {image}.{name}.{deployment}.secrets.vol:/secrets"
        " --workdir=/root"
        f" {tag} /bin/bash -c"
        ' "'
        " cp -r /dev_in/. /root/ &&"
        f" make {make_target}"
        f" {defines}"
        f" SECRETS_DIR=/secrets"
        f" BIN_PATH={bin_path}"
        f" ELF_PATH={elf_path}"
        f" EEPROM_PATH={eeprom_path}"
        '"'
    )

    logger.info(f"{tag}:{deployment}: Built device {dev_name}")

    # Package image, eeprom, and secret
    logger.info(f"{tag}:{deployment}: Packaging image for device {dev_name}")
    bin_path = dev_out / f"{dev_name}.bin"
    eeprom_path = dev_out / f"{dev_name}.eeprom"
    image_path = dev_out / f"{dev_name}.img"

    package_device(
        bin_path,
        eeprom_path,
        image_path
    )

    logger.info(f"{tag}:{deployment}: Packaged device {dev_name} image")

    return output


def package_device(
    bin_path: Path,
    eeprom_path: Path,
    image_path: Path,
):
    """
    Package a device image for use with the bootstrapper

    Accepts up to 64 bytes (encoded in hex) to insert as a secret in EEPROM
    """
    # Read input bin file
    bin_data = bin_path.read_bytes()

    # Pad bin data to max size
    image_bin_data = bin_data.ljust(FW_FLASH_SIZE, b"\xff")

    # Read EEPROM data
    eeprom_data = eeprom_path.read_bytes()

    # Pad EEPROM to max size
    image_eeprom_data = eeprom_data.ljust(FW_EEPROM_SIZE, b"\xff")

    # Create phys_image.bin
    image_data = image_bin_data + image_eeprom_data

    # Write output binary
    image_path.write_bytes(image_data)
