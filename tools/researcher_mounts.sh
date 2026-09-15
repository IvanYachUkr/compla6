# Sourced by commissioned Bash launchers. The card is evaluator-owned and sealed.
# Workbench contains only researcher files and explicitly requested recipe sources;
# host baseline preparation must finish before admission.
: "${LAB_MOUNT_WORKSPACE:?Set the initialized workspace}"
LAB_VISIBILITY=$(/usr/bin/python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get("baseline_visibility","visible"); assert v in ("visible","hidden"); print(v)' "$LAB_MOUNT_WORKSPACE/dataset-card.json")
if [[ $LAB_VISIBILITY = hidden ]]; then
  for LAB_PUBLIC_ITEM in dataset-card.json public workbench researcher-exports; do
    [[ -e $LAB_MOUNT_WORKSPACE/$LAB_PUBLIC_ITEM && ! -L $LAB_MOUNT_WORKSPACE/$LAB_PUBLIC_ITEM ]] || {
      printf '%s\n' "Missing or redirected public mount: $LAB_PUBLIC_ITEM" >&2; exit 2;
    }
  done
  LAB_WORKSPACE_MOUNTS=(
    --tmpfs "$LAB_MOUNT_WORKSPACE"
    --ro-bind "$LAB_MOUNT_WORKSPACE/dataset-card.json" "$LAB_MOUNT_WORKSPACE/dataset-card.json"
    --ro-bind "$LAB_MOUNT_WORKSPACE/public" "$LAB_MOUNT_WORKSPACE/public"
    --ro-bind "$LAB_MOUNT_WORKSPACE/workbench" "$LAB_MOUNT_WORKSPACE/workbench"
    --ro-bind "$LAB_MOUNT_WORKSPACE/researcher-exports" "$LAB_MOUNT_WORKSPACE/researcher-exports"
  )
else
  LAB_WORKSPACE_MOUNTS=(--ro-bind "$LAB_MOUNT_WORKSPACE" "$LAB_MOUNT_WORKSPACE")
fi
