import argparse
import os
import subprocess
import time
from datetime import datetime
import yaml
import select
from kube_configure_nodes import kube_configure_nodes
from mechanisms import get_mechanism_conf
import misc.spark as spark
from misc.s3 import cp_if_exists

CONFIG = 'config.yaml'
SPARK_SUBMIT = 'bin/spark-submit'
DIR_TMP = '.tmp'
OUTPUT_FILTER = 'INFO LoggingPodStatusWatcherImpl: Application status'
TELEGRAF_COLLECTION_WAIT = 10
TELEGRAF_COLLECTION_PRE_START = 10

def create_pod_templates(config, num_apps):
    
    dir_tmp_pod_templates = f"{config['dirs']['pod_templates']}/{DIR_TMP}"
    path_template_executor = config['templates']['targets']['executor_pod']['dst']

    # Create pod templates
    os.makedirs(dir_tmp_pod_templates, exist_ok=True)

    with open(path_template_executor) as file:
        template = file.read()
    for i in range(num_apps):
        with open(f"{dir_tmp_pod_templates}/executor_{i}.yaml", 'w') as file:
            file.write(template.replace("$(SPARK_APP_ID)", str(i)))

def submit_spark_apps(config, workload, num_apps, start_time, add_conf):

    dir_tmp_pod_templates = f"{config['dirs']['pod_templates']}/{DIR_TMP}"
    
    processes = []

    for i in range(num_apps):

        conf = [
            f"spark.kubernetes.executor.podTemplateFile={dir_tmp_pod_templates}/executor_{i}.yaml"
        ]

        args = [
            f"{i}",
            f"{start_time}",
        ]

        command = spark.get_submit_command(config, "workload", args, conf)
        print(command)
        processes.append(subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))

    return processes

def follow_processes(processes):
    return_codes = []

    while processes:
        for proc in processes:
            rc = proc.poll()
            if rc is not None:
                return_codes.append(rc)
                processes.remove(proc)
                continue
            
            streams, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.01)
            for stream in streams:
                output = stream.readline()
                if (output_str := output.decode()) and not OUTPUT_FILTER in output_str:
                    print(output.decode(), end='')
                
    return return_codes

def save_workload_traces(config, session_id):
    s3_bucket_traces_session = f"{config['buckets']['workload_traces']}/{session_id}/"
    
    dir_traces_session = f"{config['dirs']['data']}/{config['subdirs']['data']['workload_traces']}/{session_id}"
    
    cp_if_exists(config, s3_bucket_traces_session, dir_traces_session, "Workload Traces")

def save_from_s3_to_data(config, session_id, resource):
    s3_bucket = f"{config['buckets'][resource]}/{session_id}/"
    
    local_dir = os.path.join(
        config['dirs']['data'],
        config['subdirs']['data'][resource],
        session_id
    )
    
    cp_if_exists(config, s3_bucket, local_dir, resource)

def save_telegraf_metrics(config, session_id, script_start_time):
    dir_telegraf_session = os.path.join(
        config['dirs']['data'], 
        config['subdirs']['data']['telegraf'], 
        session_id)
    
    os.makedirs(dir_telegraf_session, exist_ok=True)

    influx_query_preamble = f'from(bucket:"telegraf") |> range(start: {script_start_time - TELEGRAF_COLLECTION_PRE_START})'

    queries = {
        'cpu': f'{influx_query_preamble} |> filter(fn: (r) => r._measurement == "cpu")',
        'mem': f'{influx_query_preamble} |> filter(fn: (r) => r._measurement == "mem" and (r._field == "used" or r._field == "available" or r._field == "total" or r._field == "free" or r._field == "shared" or r._field == "buffered"))'
    }

    for name, query in queries.items():
        execute_and_save_query(query, dir_telegraf_session, name)

def execute_and_save_query(query, dir, name):
    output_file = os.path.join(dir, f"{name}.csv")
    try:
        result = subprocess.run(['./bin/influx', 'query', query, '--raw'], check=True, capture_output=True, text=True)
        with open(output_file, 'w') as f:
            f.write(result.stdout)
        print(f"Saved {name} metrics with query \"{query}\" to '{output_file}'")
    except subprocess.CalledProcessError as e:
        print(f"Error saving {name} metrics: {e}")

def run_workload(config, workload, num_apps, start_delay, add_conf="", mechanism=None):

    if mechanism is not None:
        add_conf = ' '.join([get_mechanism_conf(config, num_apps, mechanism), add_conf])

    create_pod_templates(config, num_apps)
    kube_configure_nodes(config['kubernetes']['nodes'], num_apps)

    script_start_time = int(time.time())
    start_time = script_start_time + start_delay
    session_id = f"{workload}/{start_time}"

    processes = submit_spark_apps(config, workload, num_apps, start_time, add_conf)
    return_codes = follow_processes(processes)
    print("Workload run finished!")

    save_from_s3_to_data(config, session_id, 'workload_traces')
    save_from_s3_to_data(config, session_id, 'dynalloc_logs')
    print(f"Sleeping for {TELEGRAF_COLLECTION_WAIT} seconds before collecting telegraf metrics...")
    time.sleep(TELEGRAF_COLLECTION_WAIT)
    save_telegraf_metrics(config, session_id, script_start_time)

    return session_id

if __name__ == "__main__":
    with open(CONFIG, 'r') as file:
        config = yaml.safe_load(file)

    parser = argparse.ArgumentParser(description="Submit Spark workloads.")
    parser.add_argument("num_apps", type=int, help="Number of applications to submit")
    parser.add_argument("workload", help="Workload file path")
    parser.add_argument("start_delay", type=int, help="Delay before starting the workload")
    parser.add_argument('-m', '--mechanism', type=str, default=None, help="Mechansim to use.")
    parser.add_argument("--add_conf", default="", help="Additional Spark configuration")
    args = parser.parse_args()

    run_workload(config, args.workload, args.num_apps, args.start_delay, args.add_conf, args.mechanism)
    