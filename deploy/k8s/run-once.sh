#!/bin/sh
# Run one command in the nse-intraday namespace as a Job (same image, data volume,
# secret and security settings as the CronJobs), print its logs, then delete it.
#   sudo sh deploy/k8s/run-once.sh python -m app.ops health
set -eu
TAG=$(sudo k3s kubectl -n nse-intraday get cronjob session \
      -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}')
NAME="once-$(date +%s)"
ARGS=""
for a in "$@"; do ARGS="$ARGS\"$(printf '%s' "$a" | sed 's/"/\\"/g')\", "; done
ARGS="[${ARGS%, }]"
cat <<EOF | sudo k3s kubectl apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata: {name: $NAME, namespace: nse-intraday}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      securityContext: {runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001, fsGroup: 10001,
                        seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: once
          image: $TAG
          imagePullPolicy: Never
          command: $ARGS
          resources: {requests: {cpu: 100m, memory: 256Mi}, limits: {cpu: "1", memory: 1536Mi}}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true,
                            capabilities: {drop: ["ALL"]}}
          volumeMounts:
            - {name: data, mountPath: /app/data}
            - {name: secrets, mountPath: /secrets, readOnly: true}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - {name: data, persistentVolumeClaim: {claimName: nse-data}}
        - {name: secrets, secret: {secretName: nse-env, defaultMode: 0400}}
        - {name: tmp, emptyDir: {sizeLimit: 256Mi}}
EOF
# Poll until the Job succeeds or fails (kubectl wait with two --for flags would wait
# for BOTH conditions and never return).
i=0
while [ $i -lt 300 ]; do
  S=$(sudo k3s kubectl -n nse-intraday get job "$NAME" -o jsonpath='{.status.succeeded}{.status.failed}')
  [ -n "$S" ] && break
  sleep 2; i=$((i + 1))
done
sudo k3s kubectl -n nse-intraday logs "job/$NAME" --tail=200
STATUS=$(sudo k3s kubectl -n nse-intraday get job "$NAME" -o jsonpath='{.status.succeeded}')
sudo k3s kubectl -n nse-intraday delete job "$NAME" --wait=false >/dev/null
[ "$STATUS" = "1" ]
