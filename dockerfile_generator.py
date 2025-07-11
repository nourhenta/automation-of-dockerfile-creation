active_containers = []

import os
from dotenv import load_dotenv
load_dotenv()
DOCKERHUB_USERNAME = os.getenv("DOCKERHUB_USERNAME")
DOCKERHUB_PASSWORD = os.getenv("DOCKERHUB_PASSWORD")
          
IMAGE_NAME = "cat1"               
TAG = "latest"                          

import textwrap
from flask import Flask, render_template, request, redirect, url_for, send_file
import subprocess
import tempfile
import git
import shutil
import stat
from urllib.parse import urlparse
from werkzeug.utils import secure_filename
import xml.etree.ElementTree as ET
import re

PROJECT_PORTS = {
    'Python Flask': 4000,
    'Node.js': 3000,
    '.NET': 5000,
    'Java Maven': 8080,
    'Java Gradle': 8081,
    'Java (manual)': 8082,
    'React': 80,
    'Vite JS': 80,
    'Vanilla JS': 80,
}

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
GENERATED_FOLDER = 'generated'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GENERATED_FOLDER, exist_ok=True)
os.makedirs(os.path.join('static', 'generated'), exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['GENERATED_FOLDER'] = GENERATED_FOLDER

def ensure_docker_network_exists(network_name='app-network'):
    result = subprocess.run(["docker", "network", "ls"], capture_output=True, text=True)
    if network_name not in result.stdout:
        subprocess.run(["docker", "network", "create", network_name], check=True)

def generate_nginx_config(containers):
    config = "events {}\nhttp {\n    server {\n        listen 80;\n"
    for path, name, port in containers:
        config += f"""
        location {path} {{
            proxy_pass http://{name}:{port};
            rewrite ^{path}$ / break;
            rewrite ^{path}/(.*)$ /$1 break;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
        }}
        """
    config += "\n    }\n}"
    return config

def write_nginx_conf_file(containers):
    config = generate_nginx_config(containers)
    os.makedirs("nginx", exist_ok=True)
    with open("nginx/nginx.conf", "w") as f:
        f.write(config)

def build_and_run_nginx():
    subprocess.run(["docker", "rm", "-f", "nginx-proxy"], stderr=subprocess.DEVNULL)

    subprocess.run(["docker", "build", "-t", "nginx-proxy", "nginx"], check=True)

    subprocess.run([
        "docker", "run", "-d",
        "--name", "nginx-proxy",
        "--network", "app-network",
        "-p", "80:80",
        "nginx-proxy"
    ], check=True)

    # Check if NGINX container is running
    status = subprocess.check_output([
        "docker", "inspect", "-f", "{{.State.Status}}", "nginx-proxy"
    ]).decode().strip()

    if status != "running":
        logs = subprocess.check_output(["docker", "logs", "nginx-proxy"]).decode()
        raise RuntimeError(f"NGINX container failed to start.\nStatus: {status}\nLogs:\n{logs}")


def detect_project_type(project_path):
    for root, dirs, files in os.walk(project_path):
        if 'package.json' in files:
            with open(os.path.join(root, 'package.json')) as f:
                content = f.read()
                if 'react-scripts' in content or '"react"' in content:
                    return 'React'
            return 'Node.js'
    
        elif 'requirements.txt' in files or 'app.py' in files:
            return 'Python Flask'
        elif any(f.endswith('.csproj') for f in files):
            return '.NET'
        elif 'pom.xml' in files:
            return 'Java Maven'
        elif 'build.gradle' in files:
            return 'Java Gradle'
        elif any(f.endswith('.java') for f in files):
            return 'Java (manual)'
        elif 'vite.config.js' in files or 'vite.config.ts' in files:
            return 'Vite JS'
        elif any(f.endswith('.html') for f in files) and any(f.endswith('.js') for f in files):
            return 'Vanilla JS'

    return 'Unknown'
def detect_dotnet_target_framework(project_path):
    import re
    for file in os.listdir(project_path):
        if file.endswith(".csproj"):
            tree = ET.parse(os.path.join(project_path, file))
            root = tree.getroot()
            for tf in root.iter():
                if 'TargetFramework' in tf.tag:
                    # Return only version number, e.g., "8.0" from "net8.0"
                    match = re.search(r"net(\d+\.\d+)", tf.text)
                    return match.group(1) if match else "8.0"
    return "8.0"

def detect_dotnet_dll_name(project_path):
    for file in os.listdir(project_path):
        if file.endswith(".csproj"):
            tree = ET.parse(os.path.join(project_path, file))
            root = tree.getroot()
            ns = {'msbuild': 'http://schemas.microsoft.com/developer/msbuild/2003'}
            name = root.find('msbuild:PropertyGroup/msbuild:AssemblyName', ns)
            return name.text + ".dll" if name is not None else file.replace(".csproj", ".dll")
    return "app.dll"

def build_prompt(project_type, project_path):
    base_prompt = (
        "You are a DevOps assistant. Generate a production-ready Dockerfile for the following project. "
        "The Dockerfile should include only what is needed to build and run the app successfully inside a container. "
        "Use multi-stage builds where appropriate. Always assume the container will run in a clean environment. "
        "Do not include explanations, only the Dockerfile."
    )
    if project_type == 'Node.js':
        return base_prompt + "\n\nProject type: Node.js. Use node:18-alpine. Install dependencies. Expose 3000."
    elif project_type == 'Python Flask':
        return base_prompt + """

Project type: Python Flask.

Generate a Dockerfile with the following requirements:

1. Use `python:3.11-slim` as the base image.
2. Set `WORKDIR` to `/app`.
3. Copy the full project into the container (`COPY . .`).
4. Install dependencies using `pip install -r requirements.txt`.
5. Expose port 4000 (the Flask app listens on port 4000).
6. Use `gunicorn` as the production server with the command:
   CMD ["gunicorn", "--bind", "0.0.0.0:4000", "app:app"]

Assume `app.py` exists and contains a valid `Flask` app named `app`.
"""
    elif project_type == '.NET':
        dll_name = detect_dotnet_dll_name(project_path)
        target_framework = detect_dotnet_target_framework(project_path)
        return base_prompt + f"""
Project type: .NET.

Use the appropriate SDK and runtime based on the target framework.

Target Framework: {target_framework}
DLL Name: {dll_name}

1. Use `mcr.microsoft.com/dotnet/sdk:{target_framework}` as the build image.
2. Use `mcr.microsoft.com/dotnet/runtime:{target_framework}` as the runtime image.
3. Copy the `.csproj` file, run `dotnet restore`.
4. Copy all files and run `dotnet publish -c Release`.
5. Copy the output DLL to runtime and run with:
   ENTRYPOINT ["dotnet", "{dll_name}"]
"""

    elif project_type == 'Java Maven':
        return base_prompt + """
Project type: Java Maven.

Generate a multi-stage Dockerfile with these requirements:

1. Use `maven:3.8.6-eclipse-temurin` AS the build stage.
2. Set `WORKDIR` to `/app`.
3. Copy the full project into the build container: `COPY . .`
4. Run `mvn package -Dmaven.test.skip=true` to build the app.
5. Use `openjdk:11-jdk` as the runtime stage.
6. Set `WORKDIR` to `/app`.
7. Copy the JAR from `/app/target/*-*.jar` in the build stage to `/app/app.jar` in the runtime stage using:
   COPY --from=build-stage /app/target/*-*.jar /app/app.jar
8. Set the startup command to:
   CMD ["java", "-jar", "app.jar"]

Do **not** use ARG for JAR paths.  
Do **not** expose any ports unless the app is explicitly a web server.
"""
    elif project_type == 'Java Gradle':
        return base_prompt + "\n\nProject type: Java Gradle. Use gradle image, slim Java runtime."
    elif project_type == 'Java (manual)':
        return base_prompt + "\n\nProject type: Plain Java. Compile with javac. Run with java."
    elif project_type == 'React':
        
        return base_prompt + """
Project type: React (frontend). Use node:18-alpine to build the app, then serve the static files using nginx. Expose port 80. Use multi-stage builds.

Details:
- First stage: 
  - Use node:18-alpine
  - Set WORKDIR to /app
  - Copy package*.json
  - Run npm install
  - Copy the rest of the project
  - Run npm run build to produce production files

- Second stage:
  - Use nginx:alpine
  - Copy the contents of /app/build to /usr/share/nginx/html
  - Expose port 80
  - Use CMD ["nginx", "-g", "daemon off;"]

Do not add incorrect CMD lines like `default.conf`. Do not modify NGINX configuration unless explicitly told to.
"""
    elif project_type == 'Vite JS':
        return base_prompt + """
Project type: Vite-based JavaScript frontend (React, Vue).

Generate a multi-stage Dockerfile with the following:

1. Builder stage:
   - Use `node:18-alpine`
   - WORKDIR `/app`
   - Copy `package*.json` and run `npm install`
   - Copy project files and run `npm run build` (output in `/app/dist`)

2. Final stage:
   - Use `nginx:alpine`
   - WORKDIR `/usr/share/nginx/html`
   - Copy `/app/dist` into it
   - Copy a custom `default.conf` to `/etc/nginx/conf.d/default.conf`
   - Expose port 80
   - CMD: ["nginx", "-g", "daemon off;"]
"""
    elif project_type == 'Vanilla JS':
        return base_prompt + """
Project type: Vanilla JS.

Generate a Dockerfile that:

1. Uses `nginx:alpine` as the base image.
2. Copies all HTML, CSS, and JS files from the project folder into `/usr/share/nginx/html`.
3. Exposes port 80.
4. Uses: CMD ["nginx", "-g", "daemon off;"]

No Node.js, no npm, no build step.
"""


    else:
        return base_prompt + "\n\nProject type: Unknown. Guess based on structure."

def run_llama(prompt):
    process = subprocess.Popen(
        ["ollama", "run", "llama3"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    output, _ = process.communicate(input=prompt.encode())
    return output.decode()

def extract_dockerfile_only(text):
    lines = text.strip().splitlines()
    docker_lines = []
    in_code_block = False
    for line in lines:
        if line.strip().startswith("```dockerfile"):
            in_code_block = True
            continue
        if in_code_block and line.strip() == "```":
            break
        if in_code_block:
            docker_lines.append(line)
    if not docker_lines:
        valid_instructions = ["FROM", "WORKDIR", "COPY", "RUN", "CMD", "ENTRYPOINT", "ENV", "EXPOSE", "ARG", "LABEL", "#"]
        docker_lines = [line for line in lines if any(line.strip().startswith(i) for i in valid_instructions)]
    return "\n".join(docker_lines)


def clone_repo(url, target):
    target = os.path.normpath(target)
    if os.path.exists(target):
        shutil.rmtree(target, onerror=handle_remove_readonly)

    git.Repo.clone_from(url, target, multi_options=["--depth=1"])


def handle_remove_readonly(func, path, exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)

def build_and_run_docker_image(project_path):
    project_type = detect_project_type(project_path)
    port = PROJECT_PORTS.get(project_type, 4000)
    container_name = f"{project_type.lower().replace(' ', '-').replace('.', '')}-container"

    print(f"🛠 Building image for {project_type} on port {port} as container '{container_name}'")
    print(f"📦 Running container with: docker run -d --name {container_name} --network app-network {container_name}")

    subprocess.run(['docker', 'build', '-t', container_name, project_path], check=True)
    subprocess.run([
        'docker', 'rm', '-f', container_name
    ], stderr=subprocess.DEVNULL)
    try:
        subprocess.run([
            'docker', 'run', '-d',
            '--name', container_name,
            '--network', 'app-network',
            container_name
        ], check=True)
    except FileNotFoundError as e:
        print(f"❌ docker not found: {e.filename}")
        raise
    except subprocess.CalledProcessError as e:
        print(f"❌ Docker run failed: {e}")
        raise


    # 🔍 Check if the container is running
    status = subprocess.check_output([
        "docker", "inspect", "-f", "{{.State.Status}}", container_name
    ]).decode().strip()

    if status != "running":
        logs = subprocess.check_output(["docker", "logs", container_name]).decode()
        raise RuntimeError(f"{container_name} failed to start.\nStatus: {status}\nLogs:\n{logs}")

    return container_name, port


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/submit', methods=['POST'])
def submit():
    try:

        repo_url = request.form.get('repo_url')
        uploaded_file = request.files.get('project_folder')
        run_container = request.form.get('run_container') == 'on'
        auto_deploy_k8s = request.form.get('auto_deploy_k8s') == 'on'
        image_name = request.form.get('image_name', 'react-app').strip()
        replica_count = request.form.get('replica_count', 1)
   
        image_name = image_name.lower()
        image_name = re.sub(r'[^a-z0-9._-]', '', image_name)

        project_path = None
        github_user = None
        github_avatar = None

        if repo_url:
            parsed = urlparse(repo_url)
            path_parts = parsed.path.strip("/").split("/")
            if len(path_parts) >= 2:
                github_user = path_parts[0]
                github_avatar = f"https://github.com/{github_user}.png"


        if uploaded_file:
            filename = secure_filename(uploaded_file.filename)
            saved_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            uploaded_file.save(saved_path)
            extract_path = os.path.splitext(saved_path)[0]
            shutil.unpack_archive(saved_path, extract_path)
            project_path = extract_path

        elif repo_url:
            repo_name = os.path.splitext(os.path.basename(urlparse(repo_url).path))[0]
            repo_path = os.path.join(app.config['UPLOAD_FOLDER'], repo_name)
            clone_repo(repo_url, repo_path)
            project_path = repo_path

        if project_path:
            project_type = detect_project_type(project_path)
            prompt = build_prompt(project_type, project_path)
            response = run_llama(prompt)
            dockerfile_content = extract_dockerfile_only(response)

            dockerfile_path = os.path.join(project_path, 'Dockerfile')
            with open(dockerfile_path, 'w') as f:
                f.write(dockerfile_content)

            # 🆕 Generate Kubernetes manifest if it's a React project
            if project_type == 'React':
                manifest = textwrap.dedent(f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {image_name}-deployment
spec:
  replicas: {replica_count}
  selector:
    matchLabels:
      app: {image_name}
  template:
    metadata:
      labels:
        app: {image_name}
    spec:
      containers:
      - name: {image_name}
        image: {DOCKERHUB_USERNAME}/{image_name}:latest
        ports:
        - containerPort: 4000
---
apiVersion: v1
kind: Service
metadata:
  name: {image_name}-service
spec:
  type: NodePort
  selector:
    app: {image_name}
  ports:
    - protocol: TCP
      port: 80
      targetPort: 80
      nodePort: 30036
""")
                manifest_path = os.path.join(project_path, f"{image_name}-manifest.yaml")
                with open(manifest_path, "w") as f:
                    f.write(manifest)

                shutil.copy(manifest_path, os.path.join('static', 'generated', os.path.basename(manifest_path)))

                # 🐳 Build, tag, login, and push image to Docker Hub
                full_image = f"{DOCKERHUB_USERNAME}/{image_name}:latest"

                try:
                    print(f"👉 Building Docker image: {full_image}")
                    subprocess.run(['docker', 'build', '-t', full_image, project_path], check=True)

                    print("🔐 Logging in to Docker Hub...")
                    subprocess.run(['docker', 'login', '-u', DOCKERHUB_USERNAME, '-p', DOCKERHUB_PASSWORD], check=True)


                    print("📤 Pushing image to Docker Hub...")
                    subprocess.run(['docker', 'push', full_image], check=True)

                except FileNotFoundError as e:
                    print(f"❌ File not found: {e.filename}")
                    raise
                except subprocess.CalledProcessError as e:
                    print(f"❌ Subprocess failed: {e}")
                    raise

                # 🐳 Optional: Run the container locally and expose it via NGINX
                if run_container:
                    container_name, container_port = build_and_run_docker_image(project_path)
                    image_name = container_name
                    if not full_image.startswith("nono10/"):
                        subprocess.run(["minikube", "image", "load", container_name], check=True)
                    else:
                        print(f"📦 Skipping `minikube image load` since image is on Docker Hub: {full_image}")


                    prefix = "/" + container_name.replace("-container", "")
                    active_containers.append((prefix, container_name, container_port))
                    write_nginx_conf_file(active_containers)
                    build_and_run_nginx()

                # ☸️ Apply Kubernetes manifest if requested
                if auto_deploy_k8s and manifest_path:
                    print(f"📝 Checking if manifest exists at: {manifest_path}")
                    print("✅ Exists:", os.path.exists(manifest_path))

                    try:
                        apply_result = subprocess.run(
                            ["kubectl", "apply", "-f", manifest_path],
                            capture_output=True, text=True
                        )
                        print("📦 K8s Apply Output:", apply_result.stdout)
                        print("⚠️ K8s Apply Errors:", apply_result.stderr)
                    except FileNotFoundError as e:
                        print(f"❌ kubectl not found: {e.filename}")
                        raise

                # 📄 Copy Dockerfile to public folder for download
                public_path = os.path.join('static', 'generated', os.path.basename(dockerfile_path))
                shutil.copy(dockerfile_path, public_path)

                # ✅ Final render
                return render_template('success.html',
                    dockerfile_name=os.path.basename(dockerfile_path),
                    dockerfile_url=url_for('static', filename=f'generated/{os.path.basename(dockerfile_path)}'),
                    container_started=run_container,
                    dockerfile_content=dockerfile_content,
                    github_user=github_user,
                    github_avatar=github_avatar,
                    active_containers=active_containers if run_container else [],
                    manifest_name=os.path.basename(manifest_path) if manifest_path else "",
                    manifest_url=url_for('static', filename=f'generated/{os.path.basename(manifest_path)}') if manifest_path else "",
                )


        return redirect(url_for('index'))

    except Exception as e:
        print(f"💥 Unhandled error: {e}")
        return render_template('error.html', error=str(e)), 500

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
