// AI gateway CI. Runs on every branch (multibranch job `ai-gateway`); only `main` publishes.
// The Python project lives in gateway/; the image is built from the repo root (it bundles web/).
// CI never talks to the cluster: its only output is an image in GHCR and a Git commit in the
// GitOps repo. ArgoCD does the deploy. Rollback = `git revert` in the GitOps repo.
//
// Plugins: no `junit` (not installed, and we don't change plugins) -> JUnit XML is archived.
// Quick checks
// Lint, quick tests, eval gate, build image, bump GitOps (deploy dev)

def podYaml(String kanikoMemory) {
  return """
apiVersion: v1
kind: Pod
spec:
  containers:
    - name: python
      image: python:3.12-slim
      # PID 1 must handle SIGTERM, otherwise the pod always waits the full grace period (lesson 6)
      command: ["sh", "-c", "trap 'exit 0' TERM; sleep infinity & wait"]
      resources:
        requests: { cpu: 500m, memory: 512Mi }
        limits:   { memory: 1536Mi }
    - name: kaniko
      image: martizih/kaniko:v1.26.6-debug    # maintained fork; Google archived Kaniko (lesson 11)
      command: ["/busybox/cat"]
      tty: true
      resources:
        requests: { cpu: 500m, memory: 128Mi }    # must be <= the limit, or the API server rejects the pod
        limits:   { memory: ${kanikoMemory} }
      volumeMounts:
        - { name: docker-config, mountPath: /kaniko/.docker }
  volumes:
    - name: docker-config
      secret:
        secretName: ghcr-creds
        optional: true                 # branch builds don't push, so they don't need it
        items: [{ key: .dockerconfigjson, path: config.json }]
"""
}

pipeline {
  agent {
    kubernetes {
      yaml podYaml(params.KANIKO_MEMORY ?: '2Gi')
      defaultContainer 'python'
    }
  }
  parameters {
    string(name: 'KANIKO_MEMORY', defaultValue: '2Gi',
           description: 'Kaniko memory limit (the image also runs npm ci + vite build). Drill: 150Mi -> OOMKilled (exit 137).')
  }
  options {
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20', artifactNumToKeepStr: '10'))
    disableConcurrentBuilds()
  }
  environment {
    REGISTRY    = 'ghcr.io/ducthangng'
    IMAGE       = 'ai-gateway'
    GITOPS_REPO = 'github.com/ducthangng/mlops-router-gitops.git'
    UV_LINK_MODE = 'copy'
  }
  stages {
    stage('Setup') {
      steps {
        dir('gateway') { sh 'pip install --quiet uv==0.11.28 && uv sync --frozen && mkdir -p reports' }
      }
    }
    stage('Checks') {
      parallel {
        stage('Lint') {
          steps { dir('gateway') { sh 'uv run ruff check . --output-format=concise' } }
        }
        stage('Quick tests') {
          steps {
            // quick_tests/: 3 small demo tests (JJ routing, Redis cache); the rest is in gateway/deprecated/
            dir('gateway') { sh 'uv run pytest -q --junitxml=reports/junit-quick-tests.xml' }
          }
        }
      }
    }
    stage('Routing eval gate') {
      steps {
        // JJ (rules router): held-out test split accuracy must be >= evals/baseline.json - 2 points
        dir('gateway') {
          sh 'uv run python -m evals.run --split test --gate evals/baseline.json --out reports/eval.json'
        }
      }
    }
    stage('Build image') {
      steps {
        script { env.TAG = env.GIT_COMMIT.take(7) }   // set by the checkout; python image has no git
        container(name: 'kaniko', shell: '/busybox/sh') {
          sh '''#!/busybox/sh
            set -e
            mkdir -p "$WORKSPACE/gateway/reports"
            if [ "$BRANCH_NAME" = "main" ]; then
              PUSH="--destination $REGISTRY/$IMAGE:$TAG --cache=true --cache-repo $REGISTRY/$IMAGE-cache"
            else
              PUSH="--no-push"             # branches: prove the Dockerfile builds, publish nothing
            fi
            start=$(date +%s)
            /kaniko/executor --context "$WORKSPACE" --dockerfile gateway/Dockerfile $PUSH \
              --snapshot-mode=redo --compressed-caching=false --digest-file "$WORKSPACE/gateway/reports/image.digest"
            echo "{\\"tag\\": \\"$TAG\\", \\"branch\\": \\"$BRANCH_NAME\\", \\"build_seconds\\": $(( $(date +%s) - start ))}" \
              > "$WORKSPACE/gateway/reports/build.json"
            cat "$WORKSPACE/gateway/reports/build.json"
          '''
        }
      }
    }
    stage('Bump GitOps (dev)') {
      when { branch 'main' }
      steps {
        container('jnlp') {            // the inbound-agent image has git
          withCredentials([usernamePassword(credentialsId: 'github-pat',
                           usernameVariable: 'GH_USER', passwordVariable: 'GH_TOKEN')]) {
            sh '''
              set -e
              rm -rf gitops
              git clone --depth 1 "https://$GH_USER:$GH_TOKEN@$GITOPS_REPO" gitops
              cd gitops
              sed -i "s|^  tag: .*|  tag: \\"$TAG\\"|" charts/ai-gateway/values-dev.yaml
              git config user.name  "jenkins"
              git config user.email "jenkins@local"
              if git diff --quiet; then echo "dev already at $TAG"; exit 0; fi
              git commit -am "ai-gateway: dev -> $TAG"
              for i in 1 2 3; do git push && exit 0; git pull --rebase; done
              exit 1
            '''
          }
        }
      }
    }
  }
  post {
    always {
      archiveArtifacts artifacts: 'gateway/reports/**', allowEmptyArchive: true
    }
  }
}
