# Databricks notebook source
from pyspark.sql import functions as F
from delta.tables import DeltaTable

# COMMAND ----------

# MAGIC %run /Workspace/Users/naveengowda7796@gmail.com/consolidated_pipeline/setup/Utilities

# COMMAND ----------

print(bronze_schema,silver_schema,gold_schema)

# COMMAND ----------

dbutils.widgets.text("catalog","fmcg","Catalog")
dbutils.widgets.text("data_source","customers","Data Source")

# COMMAND ----------

catalog = dbutils.widgets.get("catalog")
data_source = dbutils.widgets.get("data_source")

base_path = f's3://sportsbar-nm/{data_source}/*.csv'
print(base_path)

# COMMAND ----------

df = (
    spark.read.format("csv")
                .option("header", True)
                .option("inferSchema", True)
                .load(base_path)
                .withColumn("read_timestamp", F.current_timestamp())
                .select("*", "_metadata.file_name","_metadata.file_size")
)                
display(df.limit(10))

# COMMAND ----------

df.printSchema()


# COMMAND ----------

df.write\
    .format("delta") \
    .option("delta.enableChangeDataFeed", "true") \
    .mode("overwrite") \
    .saveAsTable(f"{catalog}.{bronze_schema}.{data_source}")

# COMMAND ----------

# MAGIC %md
# MAGIC Silver Processing

# COMMAND ----------

df_bronze = spark.sql(f"SELECT * FROM {catalog}.{bronze_schema}.{data_source};")
df_bronze.show(10)
                      

# COMMAND ----------

df_bronze.printSchema()

# COMMAND ----------

df_duplicates = df_bronze.groupBy("customer_id").count().where("count >1")
display(df_duplicates)

# COMMAND ----------

print('Rows before duplicates dropped: ',df_bronze.count())
df_silver = df_bronze.dropDuplicates(["customer_id"])
print('Rows after duplicates dropped: ',df_silver.count())



# COMMAND ----------

# display values with leading spaces
display(
    df_silver.filter(F.col("customer_name") != F.trim(F.col("customer_name")))
)

# COMMAND ----------

df_silver = df_silver.withColumn(
    "customer_name",
    F.trim(F.col("customer_name"))
)

# COMMAND ----------

# display values with leading spaces
display(
    df_silver.filter(F.col("customer_name") != F.trim(F.col("customer_name")))
)

# COMMAND ----------

df_silver.select('city').distinct().show()

# COMMAND ----------

city_mapping = {
    'Bengaluruu': 'Bengaluru',
    'Bengalore': 'Bengaluru',

    'Hyderabadd': 'Hyderabad',
    'Hyderbad': 'Hyderabad',
    
    'NewDelhi': 'New Delhi',
    'NewDheli': 'New Delhi',
    'NewDelhee': 'New Delhi'
    }

allowed = ["Bengaluru", "Hyderabad", "New Delhi"]

df_silver = (
    df_silver.replace(city_mapping, subset = ["city"])
    .withColumn(
        "city",
        F.when(F.col("city").isNull(),None)
        .when(F.col("city").isin(allowed), F.col("city"))
        .otherwise(None)
    )
)

#sanity check
df_silver.select('city').distinct().show()

# COMMAND ----------

# title case fix
df_silver = df_silver.withColumn(
    "customer_name",
    F.when(F.col("customer_name").isNull(),None)
    .otherwise(F.initcap("customer_name"))
)

df_silver.select('customer_name').distinct().show()


# COMMAND ----------

df_silver.filter(F.col("city").isNull()).show(truncate = False)

# COMMAND ----------

null_customer_names = ["Sprintx Nutrition", "Zenathlete Foods","Primefuel Nutrition", "Recovery Lane"]
df_silver.filter(F.col("customer_name").isin(null_customer_names)).show(truncate = False)


# COMMAND ----------

#Business confirmation note: City corrections confirmed by business team
customer_city_fix = {
    #Sprintx Nutrition
    789403: "New Delhi",

   #Zenathlete Foods 
    789420: "Bengaluru",

    # Primefuel Nutrition
    789521: "Hyderabad",

    #Recovery Lane 
    789603: "Hyderabad"
}

df_fix = spark.createDataFrame(
    [(k,v) for k,v in customer_city_fix.items()],
    ["customer_id", "fixed_city"]
)
display(df_fix)


# COMMAND ----------

df_silver = (
    df_silver
    .join(df_fix, "customer_id","left")
    .withColumn(
        "city",
        F.coalesce("city", "fixed_city")  #replace null with fixed city
    )
    .drop("fixed_city")
)

display(df_silver)

# COMMAND ----------

df_silver =df_silver.withColumn("customer_id", F.col("customer_id").cast("string"))
print(df_silver.printSchema())

# COMMAND ----------

df_silver = (
    df_silver
    # build final customer column: "Customer-City" or "CustomerName-Unknown"
    .withColumn(
        "customer",
        F.concat_ws("-","customer_name", F.coalesce(F.col("city"), F.lit("Unknown")))
    )

    #static attributes aligned with parent data model
    .withColumn("market", F.lit("India"))
    .withColumn("platform", F.lit("Sports Bar"))
    .withColumn("channel", F.lit("Acquisition"))
)
display(df_silver.limit(5))

# COMMAND ----------

df_silver.write\
    .format("delta") \
    .option("delta.enableChangeDataFeed", "true") \
    .option("mergeSchema", "true") \
    .mode("overwrite") \
    .saveAsTable(f"{catalog}.{silver_schema}.{data_source}")

# COMMAND ----------

# MAGIC %md
# MAGIC Gold processing

# COMMAND ----------

df_silver = spark.sql(f"SELECT * FROM {catalog}.{silver_schema}.{data_source};")

#take required columns only
#customer_id, customer_name, city, read_timestamp,file_name,file_size, customer, market, platform, channel"
df_gold = df_silver.select("customer_id", "customer_name", "city", "customer", "market", "platform", "channel")
display(df_gold.limit(10))

# COMMAND ----------

df_gold.write\
    .format("delta") \
    .option("delta.enableChangeDataFeed", "true") \
    .mode("overwrite") \
    .saveAsTable(f"{catalog}.{gold_schema}.{data_source}")

# COMMAND ----------

delta_table = DeltaTable.forName(spark,"fmcg.gold.dim_customers")
df_child_customers = spark.table("fmcg.gold.sb_dim_customers").select(
    F.col("customer_id").alias("customer_code"),
    "customer",
    "market",
    "platform",
    "channel"
)


# COMMAND ----------

delta_table.alias("target").merge(
    source=df_child_customers.alias("source"),
    condition = "target.customer_code = source.customer_code"
).whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()